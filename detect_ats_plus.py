#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
detect_ats_plus.py

Deuxieme passe, sur les entreprises restees "inconnu".

CE QUE FAIT CETTE VERSION EN PLUS
  1. Workday complet : 9 pods, 30 noms de site, 6 identifiants
  2. SuccessFactors serieusement sonde (le grand absent jusqu'ici)
  3. Taleo elargi a tous ses prefixes usuels
  4. LES PORTAILS MAISON : on part du domaine officiel de l'entreprise,
     on teste carrieres./emploi./jobs./recrutement., on suit les
     redirections et on lit la signature de l'ATS cachee derriere.
     C'est la que se trouvent SNCF, RATP, La Poste, EDF, ADP...
     Beaucoup de portails "maison" ne sont qu'une facade devant un
     Taleo, un SuccessFactors ou un Cornerstone.

Long (2 a 4 h). Sauvegarde a chaque entreprise, donc relançable
sans rien reperdre.

  python -u detect_ats_plus.py
  python -u detect_ats_plus.py --test SNCF
  python -u detect_ats_plus.py --priorite 1
"""

import csv
import re
import sys
import time
import unicodedata
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from domaines import domaine

CSV = 'entreprises-cibles.csv'
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/124.0 Safari/537.36')
HEADERS = {'User-Agent': UA,
           'Accept': 'text/html,application/json,*/*',
           'Accept-Language': 'fr-FR,fr;q=0.9,en;q=0.8'}
TIMEOUT = 12
PARALLELISME = 8          # plus prudent : on tape des sites institutionnels
PAUSE = 0.3


# ==================================================================
# Signatures ATS, cherchees dans l'URL finale ET dans le HTML
# ==================================================================

SIGNATURES = [
    ('workday',        r'([a-z0-9\-]+)\.(?:wd\d+\.)?myworkdayjobs\.com'),
    ('workday',        r'/wday/cxs/([a-z0-9\-]+)/'),
    ('successfactors', r'career\d*\.successfactors\.(?:eu|com)[^"\']*?company=([A-Za-z0-9]+)'),
    ('successfactors', r'performancemanager\d*\.successfactors\.(?:eu|com)[^"\']*?company=([A-Za-z0-9]+)'),
    ('successfactors', r'[?&]company=([A-Za-z0-9]{3,})'),
    ('successfactors', r'(?:([a-z0-9\-]+)\.)?jobs\.sap\.com'),
    ('successfactors', r'(successfactors)\.(?:eu|com)'),
    ('taleo',          r'([a-z0-9\-]+)\.taleo\.net'),
    ('taleo',          r'(taleo)\.net'),
    ('avature',        r'([a-z0-9\-]+)\.avature\.net'),
    ('smartrecruiters', r'smartrecruiters\.com/([A-Za-z0-9\-]+)'),
    ('icims',          r'([a-z0-9\-]+)\.icims\.com'),
    ('cornerstone',    r'([a-z0-9\-]+)\.csod\.com'),
    # Le motif "embed" doit passer AVANT le motif generique, sinon
    # l'identifiant capture serait litteralement "embed".
    ('greenhouse',     r'greenhouse\.io/embed/job_board\?for=([a-z0-9\-]+)'),
    ('greenhouse',     r'(?:job-)?boards(?:\.eu)?\.greenhouse\.io/(?!embed)([a-z0-9\-]+)'),
    ('lever',          r'jobs\.lever\.co/([a-z0-9\-]+)'),
    ('ashby',          r'jobs\.ashbyhq\.com/([a-z0-9\-]+)'),
    ('talentsoft',     r'([a-z0-9\-]+)\.(?:talentsoft|talent-soft)\.com'),
    ('teamtailor',     r'([a-z0-9\-]+)\.teamtailor\.com'),
    ('recruitee',      r'([a-z0-9\-]+)\.recruitee\.com'),
    ('workable',       r'apply\.workable\.com/([a-z0-9\-]+)'),
    ('flatchr',        r'([a-z0-9\-]+)\.flatchr\.io'),
    ('beetween',       r'([a-z0-9\-]+)\.beetween\.com'),
    ('softgarden',     r'([a-z0-9\-]+)\.softgarden\.io'),
    ('jobvite',        r'jobs\.jobvite\.com/([a-z0-9\-]+)'),
    ('successfactors', r'sfcareer|jobreqcareer'),
]


def chercher_signature(texte):
    for ats, motif in SIGNATURES:
        m = re.search(motif, texte or '', re.I)
        if m:
            # Un groupe optionnel non capture renvoie None : on prend
            # le premier groupe reellement rempli, sinon chaine vide.
            t = next((g for g in m.groups() if g), '') if m.groups() else ''
            return ats, t.lower()
    return None, ''


# ==================================================================
# Identifiants candidats
# ==================================================================

BRUIT = ('france', 'french', 'groupe', 'group', 'sa', 'sas', 'societe')

# Abreviations usuelles que la derivation automatique ne trouve pas
ALIAS = {
    'Societe Generale': ['socgen', 'sg', 'societegenerale'],
    'Credit Agricole': ['ca', 'credit-agricole-sa', 'creditagricole'],
    'Groupe ADP': ['adp', 'groupeadp', 'parisaeroport', 'aeroportsdeparis'],
    'Aeroports de Paris': ['adp', 'groupeadp', 'parisaeroport'],
    'La Poste': ['laposte', 'groupelaposte', 'lapostegroupe'],
    'Francaise des Jeux': ['fdj', 'groupefdj'],
    'Les Mousquetaires': ['mousquetaires', 'itm', 'intermarche'],
    'Systeme U': ['systemeu', 'magasinsu', 'u'],
    'E.Leclerc': ['leclerc', 'eleclerc', 'galec'],
    'Caisse des Depots': ['caissedesdepots', 'cdc', 'groupecaissedesdepots'],
    'Banque des Territoires': ['banquedesterritoires', 'cdc'],
    'Air France-KLM': ['airfranceklm', 'airfrance', 'klm'],
    'Dassault Systemes': ['3ds', 'dassaultsystemes'],
    'Schneider Electric': ['schneider', 'schneiderelectric', 'se'],
    'Saint-Gobain': ['saintgobain', 'sgobain'],
    'Bureau Veritas': ['bureauveritas', 'bv'],
    'Credit Mutuel': ['creditmutuel', 'cm', 'cmcic'],
    "Caisse d'Epargne": ['caissedepargne', 'ce', 'bpce'],
    'Banque Populaire': ['banquepopulaire', 'bp', 'bpce'],
    'La Banque Postale': ['labanquepostale', 'lbp'],
    'Pro BTP': ['probtp'],
    'AG2R La Mondiale': ['ag2rlamondiale', 'ag2r'],
    'Malakoff Humanis': ['malakoffhumanis', 'malakoff'],
    'Harmonie Mutuelle': ['harmoniemutuelle', 'vyv'],
    'Mutuelle Generale': ['lamutuellegenerale', 'lmg'],
    'Urssaf Caisse Nationale': ['urssaf', 'acoss'],
    'CNAM': ['ameli', 'assurancemaladie', 'cnam'],
    'Procter & Gamble France': ['pg', 'procterandgamble'],
    'Johnson & Johnson France': ['jnj', 'johnsonandjohnson'],
    'Coca-Cola Europacific France': ['cocacolaep', 'ccep'],
    'Mondelez France': ['mondelez', 'mondelezinternational'],
    'Les Echos-Le Parisien': ['lesechosleparisien', 'lesechos'],
    'GE HealthCare France': ['gehealthcare', 'ge'],
    'Siemens Healthineers France': ['siemenshealthineers', 'siemens'],
    'Becton Dickinson France': ['bd', 'bectondickinson'],
    'IFP Energies Nouvelles': ['ifpen', 'ifpenergiesnouvelles'],
    'Voies Navigables de France': ['vnf'],
    'Unibail-Rodamco-Westfield': ['urw', 'unibail'],
    'Cushman & Wakefield France': ['cushmanwakefield', 'cushwake'],
    'ENGIE Solutions': ['engiesolutions', 'engie'],
    'Orange Business': ['orangebusiness', 'orange'],
    'BNP Paribas Real Estate': ['bnpparibasrealestate', 'bnppre'],
}


def sans_accents(s):
    s = unicodedata.normalize('NFD', s or '')
    return ''.join(c for c in s if unicodedata.category(c) != 'Mn')


def slugs(nom, maxi=6):
    out = []

    def ajoute(v):
        if v and len(v) >= 2 and v not in out:
            out.append(v)

    for a in ALIAS.get(nom.strip(), []):
        ajoute(a)

    n = sans_accents(nom).lower().replace('&', ' and ').replace('+', ' ')
    mots = [m for m in re.split(r'[^a-z0-9]+', n) if m]
    ajoute(''.join(mots))
    ajoute('-'.join(mots))
    utiles = [m for m in mots if m not in BRUIT]
    if utiles != mots and utiles:
        ajoute(''.join(utiles))
        ajoute('-'.join(utiles))
    elif len(utiles) >= 2:
        ajoute(''.join(utiles[:2]))

    # Le domaine sans extension est souvent le tenant
    d = domaine(nom)
    if d:
        ajoute(d.split('.')[0].replace('-', ''))
        ajoute(d.split('.')[0])
    return out[:maxi]


# ==================================================================
# Requetes
# ==================================================================

def _get(url, json_attendu=False):
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT,
                         allow_redirects=True)
    except requests.RequestException:
        return None
    if r.status_code >= 400:
        return None
    if json_attendu:
        try:
            r.json()
        except ValueError:
            return None
    return r


def _post(url, corps):
    try:
        h = dict(HEADERS)
        h['Content-Type'] = 'application/json'
        r = requests.post(url, headers=h, json=corps, timeout=TIMEOUT)
        if r.status_code >= 400:
            return None
        return r.json()
    except (requests.RequestException, ValueError):
        return None


# ==================================================================
# 1. Workday, version complete
# ==================================================================

WD_POD = ('wd3', 'wd5', 'wd1', 'wd2', 'wd103', 'wd12', 'wd101', 'wd10', 'wd105')


def sites_workday(t):
    T = t.capitalize()
    U = t.upper()
    return [
        'External', 'Careers', 'careers', 'carrieres', 'Carrieres',
        f'{T}Careers', f'{T}_Careers', f'{U}_Careers', f'{t}_Careers',
        f'{U}Careers', f'{t}careers',
        'ExternalCareerSite', f'{T}ExternalCareerSite',
        'External_Career_Site', 'External_Careers', 'externalcareersite',
        f'{T}_External', f'{T}_External_Career_Site',
        f'{T}_Careers_FR', f'{T}_Carrieres', 'CareerSite', f'{T}_CareerSite',
        'jobs', 'Jobs', f'{T}_Jobs', 'Recrutement', f'{T}_Recrutement',
        'Emplois', t, f'{t}-careers', f'{T}_Talent',
    ]


def sonde_workday(nom, sl):
    for pod in WD_POD:
        base = f'https://{sl}.{pod}.myworkdayjobs.com'
        for site in sites_workday(sl):
            d = _post(f'{base}/wday/cxs/{sl}/{site}/jobs',
                      {'appliedFacets': {}, 'limit': 20,
                       'offset': 0, 'searchText': ''})
            if d and d.get('jobPostings'):
                return 'workday', sl, f'{base}/{site}'
    return None


# ==================================================================
# 2. SuccessFactors
# ==================================================================

SF_HOTES = ('career5.successfactors.eu', 'career4.successfactors.eu',
            'career2.successfactors.eu', 'career10.successfactors.eu',
            'performancemanager5.successfactors.eu',
            'performancemanager4.successfactors.eu',
            'career5.successfactors.com', 'career4.successfactors.com',
            'jobs.sap.com')


def sonde_successfactors(nom, sl):
    for hote in SF_HOTES:
        for chemin in (f'/career?company={sl}',
                       f'/careers?company={sl}',
                       f'/sfcareer/jobreqcareer?company={sl}'):
            r = _get(f'https://{hote}{chemin}')
            if not r:
                continue
            corps = r.text[:200000]
            if re.search(r'jobreq|joblist|careersection|job-?title|offre',
                         corps, re.I):
                return 'successfactors', sl, r.url
        time.sleep(PAUSE)
    return None


# ==================================================================
# 3. Taleo, tous prefixes
# ==================================================================

def sonde_taleo(nom, sl):
    for hote in (f'{sl}.taleo.net', f'{sl}fr.taleo.net',
                 f'careers{sl}.taleo.net', f'{sl}group.taleo.net',
                 f'{sl}-fr.taleo.net'):
        for chemin in ('/careersection/2/moresearch.ftl',
                       '/careersection/ex/moresearch.ftl',
                       '/careersection/1/moresearch.ftl',
                       '/careersection/10000/moresearch.ftl'):
            r = _get(f'https://{hote}{chemin}')
            if r and 'careersection' in r.url.lower():
                return 'taleo', sl, r.url
    return None


# ==================================================================
# 4. LES PORTAILS MAISON  <-- le gros gisement
# ==================================================================

SOUS_DOMAINES = ('carrieres', 'careers', 'jobs', 'emploi', 'emplois',
                 'recrutement', 'talent', 'talents', 'rejoignez-nous',
                 'work', 'career', 'job', 'hr', 'rh')

CHEMINS = ('/carrieres', '/fr/carrieres', '/fr-fr/carrieres', '/carriere',
           '/nous-rejoindre', '/fr/nous-rejoindre', '/rejoignez-nous',
           '/recrutement', '/fr/recrutement', '/emploi', '/emplois',
           '/offres-emploi', '/nos-offres', '/offres',
           '/careers', '/fr/careers', '/en/careers', '/jobs', '/fr/jobs',
           '/group/careers', '/about/careers', '/en/jobs', '/talent', '')

LIENS_CARRIERE = re.compile(
    r'carri[eè]re|recrutement|nous[- ]rejoindre|rejoignez|offres?[- ]d.emploi|'
    r'\bemplois?\b|\bjobs?\b|career|talent|postuler|candidat',
    re.I)


def sonde_portail_maison(nom, sl):
    """
    Part du domaine officiel, teste les sous-domaines et chemins carrieres,
    suit les redirections, et cherche la signature de l'ATS dans l'URL
    finale puis dans le HTML. Si rien, suit les liens "carrieres" de la page.
    """
    d = domaine(nom)
    if not d:
        return None

    candidats = [f'https://{s}.{d}' for s in SOUS_DOMAINES]
    candidats += [f'https://www.{d}{c}' for c in CHEMINS]

    vus = set()
    for url in candidats:
        r = _get(url)
        if not r:
            continue

        ats, tenant = chercher_signature(r.url)
        if ats:
            return ats, (tenant or sl), r.url

        corps = r.text[:400000]
        ats, tenant = chercher_signature(corps)
        if ats:
            return ats, (tenant or sl), r.url

        # Suivre les liens "carrieres" trouves sur la page (un seul niveau)
        for m in re.finditer(r'href=["\']([^"\']{4,300})["\']', corps, re.I):
            lien = m.group(1)
            if not LIENS_CARRIERE.search(lien):
                continue
            if lien.startswith('//'):
                lien = 'https:' + lien
            elif lien.startswith('/'):
                lien = f'https://{r.url.split("/")[2]}{lien}'
            elif not lien.startswith('http'):
                continue
            if lien in vus or len(vus) > 25:
                continue
            vus.add(lien)

            ats, tenant = chercher_signature(lien)
            if ats:
                return ats, (tenant or sl), lien

            r2 = _get(lien)
            if not r2:
                continue
            ats, tenant = chercher_signature(r2.url)
            if not ats:
                ats, tenant = chercher_signature(r2.text[:400000])
            if ats:
                return ats, (tenant or sl), r2.url

        time.sleep(PAUSE)
    return None


# ==================================================================

SONDES = [
    ('workday', sonde_workday),
    ('successfactors', sonde_successfactors),
    ('taleo', sonde_taleo),
    ('portail maison', sonde_portail_maison),
]


def analyser(nom, verbeux=False):
    for sl in slugs(nom):
        for etiquette, sonde in SONDES:
            try:
                res = sonde(nom, sl)
            except Exception as e:
                if verbeux:
                    print(f'    {etiquette:16s} "{sl}" erreur {type(e).__name__}')
                continue
            if verbeux:
                print(f'    {etiquette:16s} "{sl}" -> {res or "-"}')
            if res:
                return res
    return None


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
        print(f'Domaine : {domaine(nom)}')
        print(f'Identifiants : {slugs(nom)}\n')
        res = analyser(nom, verbeux=True)
        print(f'\nResultat : {res or "rien trouve"}')
        return 0 if res else 1

    priorite = None
    if '--priorite' in args:
        priorite = args[args.index('--priorite') + 1]

    rows = list(csv.DictReader(open(CSV, encoding='utf-8')))
    todo = [r for r in rows
            if r.get('ats') in ('', 'inconnu')
            and r.get('actif', 'oui') == 'oui']
    if priorite:
        todo = [r for r in todo if r.get('priorite') == priorite]

    print(f'{len(rows)} entreprises au total')
    print(f'{len(todo)} restees inconnues, deuxieme passe')
    print(f'{PARALLELISME} en parallele. Comptez 2 a 4 h.\n', flush=True)

    fait = trouve = 0
    with ThreadPoolExecutor(max_workers=PARALLELISME) as pool:
        futurs = {pool.submit(analyser, r['nom']): r for r in todo}
        for fut in as_completed(futurs):
            row = futurs[fut]
            fait += 1
            try:
                res = fut.result()
            except Exception as e:
                print(f'  [{fait:3d}/{len(todo)}] ERREUR {row["nom"]} : {e}',
                      flush=True)
                continue

            if res:
                ats, tenant, url = res
                row['ats'] = ats
                row['ats_tenant'] = tenant
                row['url_carrieres'] = url
                trouve += 1
                print(f'  [{fait:3d}/{len(todo)}] OK  {row["nom"][:34]:36s} '
                      f'{ats:16s} {tenant}', flush=True)
            else:
                print(f'  [{fait:3d}/{len(todo)}] ??  {row["nom"][:34]:36s}',
                      flush=True)

            if fait % 5 == 0:
                sauver(rows)

    sauver(rows)

    print('\n--- Repartition finale ---', flush=True)
    for ats, n in Counter(r['ats'] for r in rows).most_common():
        print(f'  {n:4d}  {ats}')
    total = sum(1 for r in rows if r['ats'] not in ('', 'inconnu'))
    print(f'\n+{trouve} cette passe')
    print(f'{total}/{len(rows)} entreprises exploitables')
    return 0


if __name__ == '__main__':
    sys.exit(main())
