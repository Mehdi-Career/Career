#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_sites.py

VA CHERCHER LE CODE SOURCE DE CHAQUE SITE CARRIERES ET ECRIT UN RAPPORT.

Pourquoi cet outil existe
-------------------------
Le conteneur de Claude n'a acces qu'a une liste blanche de domaines
(github, pypi, npm). Il ne peut donc PAS ouvrir les sites carrieres et
codait a l'aveugle. Le runner GitHub, lui, a acces a tout.

Ce script fait donc a la place de l'humain ce qui etait fait a la main :
  1. trouve la page carrieres de chaque entreprise
  2. trouve une fiche de poste
  3. telecharge le code source des deux
  4. y cherche TOUTES les signatures de plateformes connues
  5. extrait les identifiants (tenant, company=, ssoCompanyId...)
  6. extrait les appels d'API trouves dans le JavaScript
  7. ecrit tout dans audit/ , commite, et Claude lit le resultat
     directement via raw.githubusercontent.com

  python -u audit_sites.py                 # les 355
  python -u audit_sites.py --inconnues     # seulement les non detectees
  python -u audit_sites.py --test SNCF     # une seule, sortie a l'ecran
"""

import csv
import json
import os
import re
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse

import requests

from domaines import domaine

CSV = 'entreprises-cibles.csv'
DOSSIER = 'audit'
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/124.0 Safari/537.36')
HEADERS = {'User-Agent': UA,
           'Accept': 'text/html,application/xhtml+xml,application/json,*/*',
           'Accept-Language': 'fr-FR,fr;q=0.9,en;q=0.8'}
TIMEOUT = 15
PARALLELISME = 6
MAX_HTML = 2_000_000


# ------------------------------------------------------------------
# Signatures de plateformes. On cherche TOUT, on ne suppose rien.
# ------------------------------------------------------------------

PLATEFORMES = [
    ('workday',         r'([a-z0-9\-]+)\.(wd\d+)\.myworkdayjobs\.com'),
    ('workday',         r'/wday/cxs/([a-z0-9\-]+)/([^/"\']+)/jobs'),
    ('successfactors',  r'ssoCompanyId["\s:]+[\'"]([A-Za-z0-9_\-]+)'),
    ('successfactors',  r'bplte_company=([A-Za-z0-9_\-]+)'),
    ('successfactors',  r'[?&]company=([A-Za-z0-9_\-]{3,})'),
    ('successfactors',  r'(sapsf|successfactors)\.(?:eu|com)'),
    ('successfactors',  r'/services/recruiting/v1/jobs'),
    ('taleo',           r'([a-z0-9\-]+)\.taleo\.net'),
    ('avature',         r'([a-z0-9\-]+)\.avature\.net'),
    ('talentsoft',      r'([a-z0-9\-]+)\.(?:talentsoft|talent-soft)\.com'),
    ('cornerstone',     r'([a-z0-9\-]+)\.csod\.com'),
    ('icims',           r'([a-z0-9\-]+)\.icims\.com'),
    ('smartrecruiters', r'smartrecruiters\.com/([A-Za-z0-9\-]+)'),
    ('greenhouse',      r'greenhouse\.io/embed/job_board\?for=([a-z0-9\-]+)'),
    ('greenhouse',      r'(?:job-)?boards(?:\.eu)?\.greenhouse\.io/([a-z0-9\-]+)'),
    ('lever',           r'jobs\.lever\.co/([a-z0-9\-]+)'),
    ('ashby',           r'jobs\.ashbyhq\.com/([a-z0-9\-]+)'),
    ('teamtailor',      r'([a-z0-9\-]+)\.teamtailor\.com'),
    ('recruitee',       r'([a-z0-9\-]+)\.recruitee\.com'),
    ('workable',        r'apply\.workable\.com/([a-z0-9\-]+)'),
    ('talentry',        r'([a-z0-9\-]+)\.talentry\.com'),
    ('altays',          r'altays-progiciels\.com/clicnjob[^"\']*NoSociete=(\d+)'),
    ('altays',          r'altays-progiciels\.com'),
    ('flatchr',         r'([a-z0-9\-]+)\.flatchr\.io'),
    ('beetween',        r'([a-z0-9\-]+)\.beetween\.com'),
    ('softgarden',      r'([a-z0-9\-]+)\.softgarden\.io'),
    ('jobvite',         r'jobs\.jobvite\.com/([a-z0-9\-]+)'),
    ('welcometothejungle', r'welcometothejungle\.com/[a-z]{2}/companies/([a-z0-9\-]+)'),
    ('cegid',           r'([a-z0-9\-]+)\.cegid\.com'),
    ('eolia',           r'([a-z0-9\-]+)\.eolia\.fr'),
    ('digitalrecruiters', r'([a-z0-9\-]+)\.dvore\.fr|digitalrecruiters\.com'),
    ('jobaffinity',     r'jobaffinity\.fr'),
    ('taleez',          r'([a-z0-9\-]+)\.taleez\.com'),
    ('workwell',        r'([a-z0-9\-]+)\.workwell\.io'),
    ('nextjs',          r'id="__NEXT_DATA__"'),
    ('nuxt',            r'window\.__NUXT__'),
    ('algolia',         r'([A-Z0-9]{8,12})-dsn\.algolia\.net|algolia'),
]

# Points d'entree d'API reperables dans le JavaScript
MOTIFS_API = [
    r'["\'](/(?:api|services|_next/data|graphql)/[^"\'\s]{3,120})["\']',
    r'["\'](https?://[^"\'\s]{10,160}/(?:api|services|graphql)/[^"\'\s]{0,100})["\']',
    r'fetch\(\s*["\']([^"\']{5,160})["\']',
    r'axios\.(?:get|post)\(\s*["\']([^"\']{5,160})["\']',
    r'xhr\.open\(\s*["\'](?:GET|POST)["\']\s*,\s*["\']([^"\']{5,160})["\']',
]

SOUS_DOMAINES = ('jobs', 'careers', 'carrieres', 'emploi', 'emplois',
                 'recrutement', 'career', 'talent', 'recrute', 'job')

CHEMINS = ('/nos-offres', '/offres', '/carrieres', '/fr/carrieres',
           '/nous-rejoindre', '/recrutement', '/emploi', '/emplois',
           '/offres-emploi', '/careers', '/fr/careers', '/jobs', '/search', '')

LIENS_OFFRE = re.compile(
    r'href=["\']([^"\']*(?:/(?:job|offre|offer|poste|emploi|vacancy|'
    r'nos-offres|jobdetail|JobDetail|requisition)[^"\']*\d[^"\']*))["\']', re.I)


def _get(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT,
                         allow_redirects=True)
        if r.status_code >= 400:
            return None
        return r
    except requests.RequestException:
        return None


def signatures(html, url=''):
    """Toutes les plateformes reperees, avec leur identifiant."""
    blob = f'{url}\n{html}'
    trouve = {}
    for nom, motif in PLATEFORMES:
        for m in re.finditer(motif, blob, re.I):
            groupes = [g for g in m.groups() if g] if m.groups() else []
            ident = groupes[0] if groupes else ''
            trouve.setdefault(nom, set()).add(ident)
    return {k: sorted(v)[:6] for k, v in trouve.items()}


def apis(html):
    """Points d'entree d'API repérés dans le JavaScript."""
    out = set()
    for motif in MOTIFS_API:
        for m in re.finditer(motif, html):
            u = m.group(1)
            if re.search(r'\.(png|jpe?g|svg|gif|woff2?|ttf|css|ico|webp)$', u, re.I):
                continue
            if len(u) < 5:
                continue
            out.add(u)
    return sorted(out)[:40]


def trouver_page_carrieres(dom):
    """Renvoie (reponse, url) de la page carrieres, ou (None, '')."""
    essais = [f'https://{s}.{dom}' for s in SOUS_DOMAINES]
    essais += [f'https://www.{dom}{c}' for c in CHEMINS]
    for url in essais:
        r = _get(url)
        if r and len(r.text) > 2000:
            return r, r.url
    return None, ''


def trouver_offre(r_liste):
    """Suit un lien d'offre depuis la page de liste."""
    if not r_liste:
        return None, ''
    for m in LIENS_OFFRE.finditer(r_liste.text[:MAX_HTML]):
        lien = urljoin(r_liste.url, m.group(1))
        if urlparse(lien).netloc != urlparse(r_liste.url).netloc:
            continue
        r = _get(lien)
        if r and len(r.text) > 2000:
            return r, r.url
    return None, ''


def auditer(nom):
    dom = domaine(nom)
    res = {'nom': nom, 'domaine': dom, 'url_liste': '', 'url_offre': '',
           'plateformes': {}, 'apis': [], 'erreur': ''}
    if not dom:
        res['erreur'] = 'domaine inconnu'
        return res

    try:
        r_liste, u_liste = trouver_page_carrieres(dom)
        if not r_liste:
            res['erreur'] = 'page carrieres introuvable'
            return res
        res['url_liste'] = u_liste
        html = r_liste.text[:MAX_HTML]

        r_offre, u_offre = trouver_offre(r_liste)
        if r_offre:
            res['url_offre'] = u_offre
            html += '\n' + r_offre.text[:MAX_HTML]

        res['plateformes'] = signatures(html, u_liste + ' ' + u_offre)
        res['apis'] = apis(html)

        # Le code source complet est conserve pour les cas difficiles
        os.makedirs(f'{DOSSIER}/html', exist_ok=True)
        slug = re.sub(r'[^A-Za-z0-9]+', '_', nom)[:50]
        with open(f'{DOSSIER}/html/{slug}.html', 'w', encoding='utf-8') as f:
            f.write(html[:600_000])
    except Exception as e:
        res['erreur'] = f'{type(e).__name__}: {e}'
    return res


def main():
    args = sys.argv[1:]

    if '--test' in args:
        nom = ' '.join(args[args.index('--test') + 1:])
        r = auditer(nom)
        print(json.dumps(r, ensure_ascii=False, indent=2)[:6000])
        return 0

    rows = list(csv.DictReader(open(CSV, encoding='utf-8')))
    cibles = rows
    if '--inconnues' in args:
        cibles = [r for r in rows if r.get('ats') in ('', 'inconnu')]

    os.makedirs(DOSSIER, exist_ok=True)
    print(f'{len(cibles)} entreprises a auditer, {PARALLELISME} en parallele')
    print('Le code source complet est conserve dans audit/html/\n', flush=True)

    resultats = []
    with ThreadPoolExecutor(max_workers=PARALLELISME) as pool:
        futurs = {pool.submit(auditer, r['nom']): r for r in cibles}
        for i, fut in enumerate(as_completed(futurs), 1):
            r = fut.result()
            resultats.append(r)
            plats = ', '.join(f'{k}={v[0] or "?"}' for k, v in
                              list(r['plateformes'].items())[:3]) or r['erreur'] or '-'
            print(f'  [{i:3d}/{len(cibles)}] {r["nom"][:32]:34s} {plats[:70]}',
                  flush=True)
            if i % 10 == 0:
                ecrire(resultats)
            time.sleep(0.1)

    ecrire(resultats)
    return 0


def ecrire(resultats):
    with open(f'{DOSSIER}/rapport.json', 'w', encoding='utf-8') as f:
        json.dump(resultats, f, ensure_ascii=False, indent=1)

    # Version lisible, compacte, que Claude lit en une fois
    lignes = ['# Audit des sites carrieres', '']
    compte = Counter()
    for r in sorted(resultats, key=lambda x: x['nom']):
        for p in r['plateformes']:
            compte[p] += 1
    lignes.append('## Plateformes rencontrees')
    for p, n in compte.most_common():
        lignes.append(f'- {p} : {n}')
    lignes.append('')
    lignes.append('## Detail par entreprise')
    for r in sorted(resultats, key=lambda x: x['nom']):
        lignes.append(f"\n### {r['nom']}")
        if r['erreur']:
            lignes.append(f"  ERREUR : {r['erreur']}")
        if r['url_liste']:
            lignes.append(f"  liste : {r['url_liste']}")
        if r['url_offre']:
            lignes.append(f"  offre : {r['url_offre']}")
        for p, ids in r['plateformes'].items():
            lignes.append(f"  {p} : {', '.join(i for i in ids if i) or '(sans id)'}")
        if r['apis']:
            lignes.append(f"  apis : {' | '.join(r['apis'][:12])}")
    with open(f'{DOSSIER}/rapport.md', 'w', encoding='utf-8') as f:
        f.write('\n'.join(lignes))
    print(f'\n-> {DOSSIER}/rapport.md et {DOSSIER}/rapport.json ecrits', flush=True)


if __name__ == '__main__':
    sys.exit(main())
