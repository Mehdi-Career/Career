#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
trouver_urls.py

TROUVE L'URL DU SITE CARRIERES DE CHAQUE ENTREPRISE, ET RIEN D'AUTRE.

Pourquoi ce script existe
-------------------------
La detection d'ATS a plafonne a 180/355 parce qu'elle essayait de deviner
l'IDENTIFIANT de l'entreprise chez chaque plateforme (le tenant Workday,
le company= SuccessFactors...). C'est indevinable dans le cas general.

Mais on n'en a pas besoin. Le connecteur universel de connecteurs.py
travaille a partir de l'URL seule : il sait lire les API JSON courantes,
le JSON embarque (Next.js, Nuxt), le schema.org/JobPosting et les liens
d'offres du HTML. Il suffit donc de trouver LA BONNE URL.

Methode
-------
Pour chaque entreprise, on construit une centaine d'URL candidates a
partir de son domaine et de son nom, selon les conventions francaises
(carrieres.X, X-recrute.com, emploi.X, jobs.X/fr...). On les teste, on
garde celle qui contient de VRAIES offres, et on ecrit dans le CSV.

Une page n'est retenue que si elle contient au moins 3 liens d'offres
distincts ou une API JSON qui repond. Une page "Nous rejoindre" purement
institutionnelle est ecartee : elle ne sert a rien au crawler.

  python -u trouver_urls.py                  # les entreprises sans ATS
  python -u trouver_urls.py --test SNCF      # une seule, verbeux
  python -u trouver_urls.py --toutes         # y compris celles deja faites
"""

import csv
import json
import re
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

try:
    from curl_cffi import requests as creq      # empreinte TLS de Chrome
    _CFFI = True
except ImportError:
    _CFFI = False

from domaines import domaines_possibles

CSV = 'entreprises-cibles.csv'
TIMEOUT = 12
PARALLELISME = 8

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/124.0 Safari/537.36')
HEADERS = {
    'User-Agent': UA,
    'Accept': 'text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8',
    'Accept-Language': 'fr-FR,fr;q=0.9,en;q=0.8',
    'Sec-Fetch-Dest': 'document',
    'Sec-Fetch-Mode': 'navigate',
    'Upgrade-Insecure-Requests': '1',
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)


# ------------------------------------------------------------------
# Construction des URL candidates
# ------------------------------------------------------------------

# Sous-domaines, par frequence decroissante sur les grands groupes francais
SOUS_DOMAINES = ('carrieres', 'careers', 'jobs', 'emploi', 'emplois',
                 'recrutement', 'recrute', 'career', 'job', 'talent',
                 'talents', 'rejoignez-nous', 'nous-rejoindre', 'rh', 'work')

# Chemins sur le domaine principal
CHEMINS = ('/carrieres', '/fr/carrieres', '/carriere', '/nos-offres',
           '/offres', '/offres-emploi', '/offres-d-emploi', '/nos-offres-emploi',
           '/nous-rejoindre', '/fr/nous-rejoindre', '/rejoignez-nous',
           '/recrutement', '/fr/recrutement', '/emploi', '/emplois',
           '/careers', '/fr/careers', '/en/careers', '/jobs', '/fr/jobs',
           '/group/careers', '/about/careers', '/talent', '/candidature',
           '/travailler-chez-nous', '/rh/offres',
           # Ajouts d'apres les sites reellement trouves a la 1re passe
           '/accueil.aspx?LCID=1036',        # signature Talentsoft/Cegid
           '/nous-rejoindre/nos-offres', '/carrieres/nos-offres',
           '/carrieres/offres', '/recrutement/offres', '/emploi/offres',
           '/rejoindre', '/rejoindre-nous', '/talents', '/nos-metiers',
           '/postuler', '/jobs/search', '/search-jobs', '/job-search',
           '/fr/nos-offres', '/fr/offres', '/fr/emploi', '/fr/recrutement',
           '/fr-fr/carrieres', '/fr-fr/careers', '/en/jobs', '/fr',
           '/recherche-offres', '/toutes-nos-offres', '/offres/recherche', '')

# Domaines dedies au recrutement : convention tres francaise
#   laposterecrute.fr, enedis-recrute.fr, groupeXrecrute.com...
def _domaines_recrutement(nom, dom):
    racine = dom.split('.')[0] if dom else ''
    ext = '.' + dom.split('.', 1)[1] if dom and '.' in dom else '.fr'
    mots = re.sub(r'[^a-z0-9]+', '', _sans_accents(nom).lower())
    out = []
    for base in {racine, mots}:
        if not base or len(base) < 3:
            continue
        for forme in (f'{base}recrute', f'{base}-recrute', f'{base}recrutement',
                      f'{base}-recrutement', f'{base}-emploi', f'{base}emploi',
                      f'{base}-carrieres', f'{base}carrieres', f'{base}-jobs',
                      f'{base}jobs', f'{base}-careers', f'{base}careers',
                      f'groupe{base}recrute', f'groupe{base}', f'{base}-talents',
                      f'recrutement-{base}', f'emploi-{base}', f'jobs-{base}'):
            for e in {ext, '.fr', '.com'}:
                out.append(forme + e)
    return out


def _sans_accents(s):
    import unicodedata
    s = unicodedata.normalize('NFD', s or '')
    return ''.join(c for c in s if unicodedata.category(c) != 'Mn')


def candidats(nom):
    """Toutes les URL a tester, des plus probables aux moins probables."""
    urls, vus = [], set()

    def ajoute(u):
        if u not in vus:
            vus.add(u)
            urls.append(u)

    doms = domaines_possibles(nom)
    for dom in doms:
        # 1. Sous-domaines dedies : c'est de loin le plus frequent
        for sd in SOUS_DOMAINES:
            ajoute(f'https://{sd}.{dom}')
        # 2. Domaines de recrutement dedies
        for d in _domaines_recrutement(nom, dom):
            ajoute(f'https://www.{d}')
            ajoute(f'https://{d}')
    for dom in doms:
        # 3. Chemins sur le site principal
        for ch in CHEMINS:
            ajoute(f'https://www.{dom}{ch}')
    return urls


# ------------------------------------------------------------------
# Verification : la page contient-elle de VRAIES offres ?
# ------------------------------------------------------------------

LIEN_OFFRE = re.compile(
    r'href=["\']([^"\']*?/(?:jobs?|offres?|offer|emplois?|poste|vacancy|'
    r'careers?|recrutement|job-detail|jobdetail|nos-offres|offre-emploi)'
    r'[/\-][^"\']*?\d{3,}[^"\']*)["\']', re.I)

MOTS_OFFRE = re.compile(
    r'\b(CDI|CDD|alternance|stage|temps plein|postuler|candidater|'
    r'voir l.offre|d[ée]poser (?:votre |mon )?cv)\b', re.I)

API_JSON = ('/services/recruiting/v1/jobs', '/api/offers/', '/api/public/jobs',
            '/api/v1/jobs', '/api/jobs', '/jobs.json')

# Signatures d'ATS : si on les voit, on nomme la plateforme au passage
SIGNATURES = [
    ('workday',         r'([a-z0-9\-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([^/?#"\']+)'),
    ('successfactors',  r'ssoCompanyId["\s:]+[\'"]([A-Za-z0-9_\-]{3,})'),
    ('successfactors',  r'[?&]company=([A-Za-z0-9_\-]{3,})'),
    ('successfactors',  r'/services/recruiting/v1/jobs'),
    ('taleo',           r'https?://([a-z0-9\-]{3,})\.taleo\.net'),
    ('avature',         r'https?://([a-z0-9\-]{3,})\.avature\.net'),
    ('talentsoft',      r'https?://([a-z0-9\-]{3,})\.(?:talentsoft|talent-soft)\.com'),
    ('cornerstone',     r'https?://([a-z0-9\-]{3,})\.csod\.com'),
    ('icims',           r'https?://([a-z0-9\-]{3,})\.icims\.com'),
    ('smartrecruiters', r'smartrecruiters\.com/([A-Za-z0-9\-]{3,})'),
    ('greenhouse',      r'greenhouse\.io/(?:embed/job_board\?for=)?([a-z0-9\-]{3,})'),
    ('lever',           r'jobs\.lever\.co/([a-z0-9\-]{3,})'),
    ('ashby',           r'jobs\.ashbyhq\.com/([a-z0-9\-]{3,})'),
    ('teamtailor',      r'https?://([a-z0-9\-]{3,})\.teamtailor\.com'),
    ('recruitee',       r'https?://([a-z0-9\-]{3,})\.recruitee\.com'),
    ('taleez',          r'https?://([a-z0-9\-]{3,})\.taleez\.com'),
    ('workable',        r'apply\.workable\.com/([a-z0-9\-]{3,})'),
]

NULS = {'www', 'app', 'tt', 'careers', 'career', 'jobs', 'job', 'files',
        'events', 'emploi', 'emplois', 'carrieres', 'recrutement', 'talent',
        'api', 'fr', 'en', 'successfactors', 'taleo', 'avature', 'workday',
        'csod', 'icims', 'embed', 'static', 'cdn'}


def signature(texte):
    """(ats, tenant) si une plateforme connue est reperee."""
    for ats, motif in SIGNATURES:
        m = re.search(motif, texte or '', re.I)
        if not m:
            continue
        t = next((g for g in m.groups() if g), '') if m.groups() else ''
        t = re.sub(r'^(?:2F|u002F|%2F)+', '', t or '', flags=re.I).lower()
        return ats, ('' if t in NULS or len(t) < 3 else t)
    return '', ''


def _get(url):
    try:
        r = SESSION.get(url, timeout=TIMEOUT, allow_redirects=True)
    except requests.RequestException:
        return None
    if r.status_code in (403, 406, 429) and _CFFI:
        try:                                  # blocage sur l'empreinte TLS
            r2 = creq.get(url, impersonate='chrome124', timeout=TIMEOUT)
            if r2.status_code < 400:
                return r2
        except Exception:
            pass
    return r if r.status_code < 400 else None


def note_page(html):
    """
    Combien d'offres reelles cette page contient-elle ?
    Une page institutionnelle "Nous rejoindre" renvoie 0 : elle est
    inutile au crawler, autant continuer a chercher.
    """
    if not html or len(html) < 800:
        return 0
    liens = {m.group(1) for m in LIEN_OFFRE.finditer(html[:1500000])}
    if len(liens) >= 3:
        return len(liens)
    # JSON embarque ou schema.org : le contenu est charge en JavaScript
    if re.search(r'"@type"\s*:\s*"JobPosting"', html):
        return 5
    if re.search(r'id="__NEXT_DATA__"|window\.__NUXT__', html) and \
       len(MOTS_OFFRE.findall(html)) >= 4:
        return 4
    return len(liens)


_MOTS_RECRUT = ('job', 'career', 'carriere', 'emploi', 'recrut',
                'recruitment', 'talent', 'rejoindre', 'candidat', 'apply',
                'offres', 'vacature', 'stellen')


def plausible(url, html):
    """
    Note FAIBLE (1 a 5) pour une page qui ressemble a un site d'offres
    sans le prouver.

    POURQUOI. note_page() exige des liens d'offres dans le HTML. Or
    jobs.veolia.com, careers.legrand.com ou jobs.suez.com chargent leurs
    offres en JavaScript : le HTML livre est une coquille vide. Elles
    tombaient a 0 et l'URL etait JETEE, alors que c'est exactement la
    bonne page et que le connecteur universel sait interroger leurs API.

    Ces pages sont donc repechees avec une note negative : enregistrees,
    mais signalees comme non prouvees. verifier_crawl.py tranche ensuite,
    en mesurant les offres reellement recuperees.
    """
    if not url or not html or len(html) < 500:
        return 0
    u = url.lower()
    morceaux = u.split('/')
    hote = morceaux[2] if len(morceaux) > 2 else ''
    chemin = '/'.join(morceaux[3:])

    if any(m in hote for m in _MOTS_RECRUT):
        score = 3              # jobs.veolia.com, careers.legrand.com
    elif any(m in chemin for m in _MOTS_RECRUT):
        score = 2              # groupe.fr/nos-offres-emploi
    else:
        return 0

    if len(set(m.group(0).lower() for m in MOTS_OFFRE.finditer(html))) >= 2:
        score += 1             # CDI, postuler, alternance... le vocabulaire y est
    if re.search(r'id="__NEXT_DATA__"|window\.__NUXT__|data-reactroot|'
                 r'ng-version=|<div id="root"|<div id="app"', html, re.I):
        score += 1             # application JS : les offres arrivent en XHR

    # Sous 3, c'est une page institutionnelle "Nous rejoindre" : ni
    # vocabulaire d'offre, ni application JS. Inutile de l'enregistrer.
    return min(score, 5) if score >= 3 else 0


def _mieux(a, b):
    """
    Garde la meilleure de deux pistes (url, ats, tenant, note).
    Une note positive (offres vues) bat toujours une note negative
    (page seulement plausible), quelle que soit l'amplitude.
    """
    cle = lambda c: (c[3] > 0, abs(c[3])) if c[0] else (False, -1)
    return a if cle(a) >= cle(b) else b


def api_repond(base):
    """Une API JSON d'offres repond-elle sur ce domaine ?"""
    for chemin in API_JSON:
        try:
            if 'recruiting' in chemin:
                r = SESSION.post(base + chemin, timeout=TIMEOUT, json={
                    'locale': 'fr_FR', 'pageNumber': 0, 'sortBy': '',
                    'keywords': '', 'location': '', 'facetFilters': {},
                    'brand': '', 'skills': [], 'categoryId': 0,
                    'alertId': '', 'rcmCandidateId': ''})
            else:
                r = SESSION.get(base + chemin, timeout=TIMEOUT)
            if r.status_code >= 400:
                continue
            d = r.json()
        except Exception:
            continue
        lot = d if isinstance(d, list) else (
            d.get('jobs') or d.get('offers') or d.get('data')
            or d.get('list') or d.get('content') or [])
        if isinstance(lot, list) and len(lot) >= 2:
            return chemin
    return ''



# ------------------------------------------------------------------
# Navigation depuis la page d'accueil
#
# LA lacune de la premiere passe : elle ne faisait que DEVINER des URL.
# Un humain, lui, ouvre le site et clique sur "Carrieres". Beaucoup de
# grands groupes ont une adresse qui ne suit aucune convention
# (edf.fr/edf-recrute, carrefour.fr/nous-rejoindre...) : seul le lien
# depuis l'accueil y mene.
#
# Deux niveaux : accueil -> page carrieres -> page qui LISTE les offres.
# La page carrieres d'un grand groupe est souvent une vitrine ; la liste
# est un cran plus loin.
# ------------------------------------------------------------------

LIEN_CARRIERE = re.compile(
    r'carri[eè]res?|recrutement|recrute|nous[\s\-]*rejoindre|rejoign|'
    r'offres?[\s\-]*d.emploi|\bemplois?\b|\bjobs?\b|career|talent|'
    r'postuler|candidat|work[\s\-]*with|travailler', re.I)

LIEN_LISTE = re.compile(
    r'nos[\s\-]*offres|toutes[\s\-]*(?:nos[\s\-]*)?offres|voir[\s\-]*les[\s\-]*offres|'
    r'rechercher?[\s\-]*(?:une[\s\-]*)?offre|offres?[\s\-]*d.emploi|'
    r'search[\s\-]*jobs?|all[\s\-]*jobs?|view[\s\-]*(?:all[\s\-]*)?jobs?|'
    r'consulter[\s\-]*(?:nos[\s\-]*)?offres|postes?[\s\-]*(?:a[\s\-]*pourvoir|ouverts)',
    re.I)


def _liens(html, url_base, motif, maxi=14):
    """Liens dont le texte OU l'adresse matche le motif."""
    out, vus = [], set()
    for m in re.finditer(
            r'<a[^>]+href=["\']([^"\']{2,400})["\'][^>]*>(.{0,200}?)</a>',
            html or '', re.I | re.S):
        href = m.group(1)
        texte = re.sub(r'<[^>]+>|\s+', ' ', m.group(2)).strip()
        if not (motif.search(texte) or motif.search(href)):
            continue
        if href.startswith(('mailto:', 'tel:', 'javascript:', '#')):
            continue
        lien = requests.compat.urljoin(url_base, href)
        if not lien.startswith('http') or lien in vus:
            continue
        vus.add(lien)
        out.append(lien)
        if len(out) >= maxi:
            break
    return out


def depuis_accueil(nom, verbeux=False):
    """
    Ouvre le site principal, suit le lien "Carrieres", puis depuis cette
    page suit le lien vers la LISTE des offres. Renvoie le meilleur
    (url, ats, tenant, note) trouve.
    """
    meilleur = ('', '', '', 0)

    for dom in domaines_possibles(nom)[:3]:
        for racine in (f'https://www.{dom}', f'https://{dom}'):
            acc = _get(racine)
            if not acc:
                continue
            html_acc = acc.text[:1200000]

            for lien in _liens(html_acc, acc.url, LIEN_CARRIERE):
                r = _get(lien)
                if not r:
                    continue
                html = r.text[:1500000]
                n = note_page(html)
                base = '/'.join(r.url.rstrip('/').split('/')[:3])
                ats, tenant = signature(r.url + '\n' + html)

                if n < 3:
                    if api_repond(base):
                        n = max(n, 6)

                cote = n if n > 0 else -plausible(r.url, html)
                if verbeux and (cote or ats):
                    etat = f'{n:2d} offre(s)' if n > 0 else f'plausible {-cote}/5'
                    print(f'    accueil-> {etat:15s} {ats or "-":15s} {r.url[:64]}')

                if cote:
                    meilleur = _mieux(meilleur,
                                      (r.url, ats or 'generique', tenant, cote))
                if n >= 6:
                    return meilleur

                # Niveau 2 : cette page est une vitrine, la liste est plus loin
                for l2 in _liens(html, r.url, LIEN_LISTE, maxi=6):
                    if l2 == r.url:
                        continue
                    r2 = _get(l2)
                    if not r2:
                        continue
                    h2 = r2.text[:1500000]
                    n2 = note_page(h2)
                    a2, t2 = signature(r2.url + '\n' + h2)
                    if n2 < 3 and api_repond('/'.join(r2.url.rstrip('/').split('/')[:3])):
                        n2 = max(n2, 6)
                    cote2 = n2 if n2 > 0 else -plausible(r2.url, h2)
                    if verbeux and cote2:
                        etat = (f'{n2:2d} offre(s)' if n2 > 0
                                else f'plausible {-cote2}/5')
                        print(f'      liste-> {etat:15s} {a2 or "-":15s} {r2.url[:62]}')
                    if cote2:
                        meilleur = _mieux(meilleur,
                                          (r2.url, a2 or 'generique', t2, cote2))
                    if n2 >= 6:
                        return meilleur
            if meilleur[3] > 0:
                return meilleur
    return meilleur


def chercher(nom, verbeux=False):
    """(url, ats, tenant, note) de la meilleure page trouvee."""
    meilleur = ('', '', '', 0)
    testes = 0
    for url in candidats(nom):
        r = _get(url)
        testes += 1
        if not r:
            continue
        html = r.text[:1500000]
        n = note_page(html)
        base = '/'.join(r.url.rstrip('/').split('/')[:3])

        ats, tenant = signature(r.url + '\n' + html)

        # Une API qui repond vaut mieux qu'une page bien remplie
        if n < 3:
            ch = api_repond(base)
            if ch:
                n = max(n, 6)
                if verbeux:
                    print(f'    API {ch} repond sur {base}')

        cote = n if n > 0 else -plausible(r.url, html)

        if verbeux and (cote or ats):
            etat = f'{n:2d} offre(s)' if n > 0 else f'plausible {-cote}/5'
            print(f'    {etat:16s} {ats or "-":16s} {r.url[:70]}')

        if cote:
            meilleur = _mieux(meilleur, (r.url, ats or 'generique', tenant, cote))
        if n >= 6:          # franchement une page d'offres : on s'arrete
            return meilleur
        if testes > 110:
            break

    # Les URL devinees n'ont rien donne de franc : on navigue depuis
    # l'accueil, comme le ferait un humain.
    if meilleur[3] < 3:
        if verbeux:
            print('    -- devinette insuffisante, navigation depuis l accueil --')
        meilleur = _mieux(meilleur, depuis_accueil(nom, verbeux))
    return meilleur


# ------------------------------------------------------------------

def sauver(rows):
    with open(CSV, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=rows[0].keys())
        w.writeheader()
        w.writerows(rows)


def main():
    args = sys.argv[1:]

    if '--test' in args:
        nom = ' '.join(args[args.index('--test') + 1:])
        print(f'Test : {nom}')
        print(f'{len(candidats(nom))} URL candidates\n')
        url, ats, tenant, n = chercher(nom, verbeux=True)
        print(f'\nRetenu : {url or "rien trouve"}')
        if n > 0:
            print(f'  ats={ats}  tenant={tenant}  {n} offre(s) vue(s)')
        elif n < 0:
            print(f'  ats={ats}  tenant={tenant}  page plausible {-n}/5, '
                  f'offres NON confirmees (verifier_crawl.py tranchera)')
        return 0 if url else 1

    rows = list(csv.DictReader(open(CSV, encoding='utf-8')))
    cibles = rows if '--toutes' in args else [
        r for r in rows if r.get('ats') in ('', 'inconnu')]
    cibles = [r for r in cibles if r.get('actif', 'oui') == 'oui']

    # Diagnostic : curl_cffi est ce qui debloque les sites qui repondent
    # 403 aux clients Python (SUEZ, BNP...). Sans lui, ils resteront perdus.
    print('=' * 60)
    if _CFFI:
        ok = 0
        for u in ('https://www.suez.com', 'https://www.carrefour.fr'):
            try:
                if creq.get(u, impersonate='chrome124', timeout=10).status_code < 400:
                    ok += 1
            except Exception:
                pass
        print(f'curl_cffi actif - {ok}/2 sites normalement bloques repondent')
    else:
        print('ATTENTION : curl_cffi absent. Les sites qui repondent 403')
        print('aux clients Python resteront introuvables (SUEZ, BNP...).')
    print('=' * 60 + '\n', flush=True)

    print(f'{len(cibles)} entreprises a traiter, {PARALLELISME} en parallele')
    print('Deux strategies : URL devinees, puis navigation depuis '
          'la page d accueil.\n', flush=True)

    trouve = repeche = 0
    with ThreadPoolExecutor(max_workers=PARALLELISME) as pool:
        futurs = {pool.submit(chercher, r['nom']): r for r in cibles}
        for i, fut in enumerate(as_completed(futurs), 1):
            row = futurs[fut]
            try:
                url, ats, tenant, n = fut.result()
            except Exception as e:
                print(f'  [{i:3d}/{len(cibles)}] ERREUR {row["nom"]} : {e}',
                      flush=True)
                continue

            if url and n >= 3:
                row['url_carrieres'] = url
                row['ats'] = ats
                row['ats_tenant'] = tenant
                trouve += 1
                print(f'  [{i:3d}/{len(cibles)}] OK  {row["nom"][:30]:32s} '
                      f'{ats:16s} {n:3d} offres  {url[:58]}', flush=True)
            elif url and n != 0:
                # Page credible mais non prouvee : offres chargees en
                # JavaScript, ou un ou deux liens seulement. On l'enregistre
                # quand meme - le connecteur universel sait interroger les
                # API - et verifier_crawl.py tranchera en mesurant.
                row['url_carrieres'] = url
                row['ats'] = ats or 'generique'
                row['ats_tenant'] = tenant
                repeche += 1
                print(f'  [{i:3d}/{len(cibles)}] ?   {row["nom"][:30]:32s} '
                      f'{(ats or "generique"):16s} a verifier   {url[:58]}',
                      flush=True)
            else:
                print(f'  [{i:3d}/{len(cibles)}] --  {row["nom"][:30]:32s} '
                      f'aucune page d offres trouvee', flush=True)

            if i % 5 == 0:
                sauver(rows)

    sauver(rows)

    print(f'\n+{trouve} entreprises debloquees cette passe (offres vues)')
    print(f'+{repeche} repechees : page credible, offres a confirmer')
    if repeche:
        print('  -> lance "Verifier le crawl" pour savoir lesquelles tiennent')
    print('\n--- Repartition ---')
    for a, n in Counter(r['ats'] for r in rows).most_common():
        print(f'  {n:4d}  {a}')
    total = sum(1 for r in rows
                if r['ats'] not in ('', 'inconnu')
                and (r['ats_tenant'] or r['url_carrieres']))
    print(f'\n{total}/{len(rows)} entreprises exploitables')
    return 0


if __name__ == '__main__':
    sys.exit(main())
