#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
detect_ats.py  -  version 2

Remplit url_carrieres, ats et ats_tenant dans entreprises-cibles.csv.

CHANGEMENT MAJEUR vs v1 : plus aucun moteur de recherche.
DuckDuckGo bloque les IP de datacenter, donc la v1 echouait a 100 %
sur les runners GitHub. On interroge maintenant DIRECTEMENT les API
des ATS en devinant l'identifiant a partir du nom de l'entreprise.

  pip install requests
  python -u detect_ats.py
"""

import csv
import re
import socket
import sys
import unicodedata
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

CSV = 'entreprises-cibles.csv'
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/124.0 Safari/537.36')
HEADERS = {'User-Agent': UA, 'Accept': 'application/json,text/html'}
TIMEOUT = 8
PARALLELISME = 12


# ------------------------------------------------------------------
# Generation des identifiants candidats
# ------------------------------------------------------------------

BRUIT = ('france', 'french', 'groupe', 'group', 'sa', 'sas', 'societe')


def sans_accents(s):
    s = unicodedata.normalize('NFD', s or '')
    return ''.join(c for c in s if unicodedata.category(c) != 'Mn')


def slugs(nom):
    """
    'Groupe ADP'          -> ['groupeadp', 'groupe-adp', 'adp']
    'Air France-KLM'      -> ['airfranceklm', 'air-france-klm', ...]
    'Kuehne+Nagel France' -> ['kuehnenagelfrance', 'kuehnenagel', 'kuehne', ...]
    """
    n = sans_accents(nom).lower()
    n = n.replace('&', ' and ').replace('+', ' ')
    mots = [m for m in re.split(r"[^a-z0-9]+", n) if m]

    out = []

    def ajoute(v):
        if v and len(v) >= 2 and v not in out:
            out.append(v)

    ajoute(''.join(mots))                 # groupeadp
    ajoute('-'.join(mots))                # groupe-adp

    utiles = [m for m in mots if m not in BRUIT]
    if utiles != mots and utiles:
        # Un mot de bruit a ete retire : 'Randstad France' -> 'randstad',
        # 'Groupe ADP' -> 'adp'. La forme courte est fiable ici.
        ajoute(''.join(utiles))
        ajoute('-'.join(utiles))
        if len(utiles) == 1:
            ajoute(utiles[0])
    elif len(utiles) >= 2:
        # Tous les mots sont significatifs : 'Credit Agricole'.
        # On NE tente PAS 'credit' seul, ce serait un faux positif assure.
        ajoute(''.join(utiles[:2]))

    return out[:5]


# ------------------------------------------------------------------
# Sondes par ATS
# ------------------------------------------------------------------

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


def sonde_greenhouse(s):
    r = _get(f'https://boards-api.greenhouse.io/v1/boards/{s}/jobs', True)
    if not r:
        return None
    jobs = r.json().get('jobs')
    if isinstance(jobs, list) and jobs:
        return 'greenhouse', s, f'https://job-boards.greenhouse.io/{s}'
    return None


def sonde_lever(s):
    r = _get(f'https://api.lever.co/v0/postings/{s}?mode=json', True)
    if not r:
        return None
    d = r.json()
    if isinstance(d, list) and d:
        return 'lever', s, f'https://jobs.lever.co/{s}'
    return None


def sonde_smartrecruiters(s):
    """
    ATTENTION : cette API renvoie 200 avec {"totalFound": 0, "content": []}
    pour une societe INEXISTANTE. Tester la presence de 'content' ne suffit
    pas, il faut exiger de vraies offres. C'est ce qui avait fait matcher
    355 entreprises sur 355 en v2.
    """
    r = _get(f'https://api.smartrecruiters.com/v1/companies/{s}/postings?limit=5', True)
    if not r:
        return None
    d = r.json()
    if d.get('totalFound', 0) > 0 and d.get('content'):
        return 'smartrecruiters', s, f'https://jobs.smartrecruiters.com/{s}'
    return None


def sonde_ashby(s):
    r = _get(f'https://api.ashbyhq.com/posting-api/job-board/{s}', True)
    if not r:
        return None
    d = r.json()
    if isinstance(d.get('jobs'), list) and d['jobs']:
        return 'ashby', s, f'https://jobs.ashbyhq.com/{s}'
    return None


# ==================================================================
# ATS a "host devinable" : Workday, Taleo, Avature, iCIMS, Cornerstone
#
# Methode : on verifie D'ABORD que le nom de domaine existe (resolution
# DNS, ~1 ms, aucune requete HTTP). 95 % des combinaisons sont eliminees
# instantanement. On ne teste les points d'entree que sur les hotes reels.
#
# L'erreur de la v2 etait de taper la RACINE du domaine : Workday y
# renvoie souvent un 404 alors que le tenant existe bel et bien.
# ==================================================================

_DNS_CACHE = {}


def hote_existe(hote):
    """Resolution DNS seule. Tres rapide, et definitive."""
    if hote in _DNS_CACHE:
        return _DNS_CACHE[hote]
    try:
        socket.setdefaulttimeout(3)
        socket.gethostbyname(hote)
        ok = True
    except (socket.gaierror, socket.timeout, OSError):
        ok = False
    _DNS_CACHE[hote] = ok
    return ok


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


# ATTENTION : *.myworkdayjobs.com utilise un DNS GENERIQUE.
# N'importe quel nom resout, y compris 'zzqxwvbogus123'. Le filtre DNS
# ne sert donc a rien ici, il faut interroger le point d'entree CXS.
# On limite le nombre de combinaisons pour rester sous le radar d'Akamai.
WD_POD = ('wd3', 'wd5', 'wd1')


def sites_workday(t):
    """
    Noms de site carrieres, verifies sur des cas reels :
      sanofi.wd3      -> SanofiCareers
      eiffage.wd3     -> Eiffage_Careers
      pernodricard.wd3-> pernod-ricard
      nvidia.wd5      -> NVIDIAExternalCareerSite
    Liste volontairement courte : 3 pods x 8 sites x 2 identifiants = 48
    requetes maximum par entreprise, ce qui reste supportable.
    """
    T = t.capitalize()
    return [
        'External', 'Careers',
        f'{T}Careers', f'{T}_Careers',
        'ExternalCareerSite', f'{T}ExternalCareerSite',
        t, f'{T}_External',
    ]


def sonde_workday(s):
    for pod in WD_POD:
        base = f'https://{s}.{pod}.myworkdayjobs.com'
        for site in sites_workday(s):
            d = _post(f'{base}/wday/cxs/{s}/{site}/jobs',
                      {'appliedFacets': {}, 'limit': 20,
                       'offset': 0, 'searchText': ''})
            if d and d.get('jobPostings'):
                return 'workday', s, f'{base}/{site}'
    return None


def sonde_taleo(s):
    hote = f'{s}.taleo.net'
    if not hote_existe(hote):
        return None
    for chemin in ('/careersection/2/moresearch.ftl',
                   '/careersection/ex/moresearch.ftl',
                   '/careersection/1/moresearch.ftl',
                   '/careersection/'):
        r = _get(f'https://{hote}{chemin}')
        if r and 'careersection' in r.url.lower():
            return 'taleo', s, r.url
    return None


def sonde_avature(s):
    hote = f'{s}.avature.net'
    if not hote_existe(hote):
        return None
    for chemin in ('/careers', '/fr_FR/careers', '/jobs', '/'):
        r = _get(f'https://{hote}{chemin}')
        if r:
            return 'avature', s, r.url
    return None


def sonde_cornerstone(s):
    hote = f'{s}.csod.com'
    if not hote_existe(hote):
        return None
    r = _get(f'https://{hote}/ux/ats/careersite/1/home?c={s}')
    if r:
        return 'cornerstone', s, r.url
    return None


def sonde_icims(s):
    for hote in (f'careers-{s}.icims.com', f'{s}.icims.com',
                 f'jobs-{s}.icims.com'):
        if not hote_existe(hote):
            continue
        r = _get(f'https://{hote}/jobs/search?ss=1')
        if r and 'icims' in r.url.lower():
            return 'icims', s, r.url
    return None


def sonde_teamtailor(s):
    if not hote_existe(f'{s}.teamtailor.com'):
        return None
    r = _get(f'https://{s}.teamtailor.com/jobs')
    if r and 'teamtailor' in r.url.lower():
        return 'teamtailor', s, r.url
    return None


def sonde_recruitee(s):
    r = _get(f'https://{s}.recruitee.com/api/offers/', True)
    if not r:
        return None
    # Un sous-domaine inexistant redirige vers recruitee.com : verifier
    # qu'on est bien reste sur le sous-domaine demande.
    if f'{s}.recruitee.com' not in r.url.lower():
        return None
    if r.json().get('offers'):
        return 'recruitee', s, f'https://{s}.recruitee.com'
    return None


def sonde_workable(s):
    r = _get(f'https://apply.workable.com/api/v1/widget/accounts/{s}', True)
    if not r:
        return None
    d = r.json()
    # Un compte Workable peut exister sans aucune offre (cas Randstad).
    # Exiger de vraies offres, sinon c'est un faux positif.
    if d.get('jobs'):
        return 'workable', s, f'https://apply.workable.com/{s}'
    return None


# Ordre : les plus courants chez les grands groupes francais d'abord
SONDES = [
    sonde_workday,
    sonde_smartrecruiters,
    sonde_greenhouse,
    sonde_lever,
    sonde_ashby,
    sonde_taleo,
    sonde_avature,
    sonde_icims,
    sonde_cornerstone,
    sonde_teamtailor,
    sonde_recruitee,
    sonde_workable,
]


def analyser(nom):
    """Renvoie (ats, tenant, url) ou (None, '', '')."""
    for s in slugs(nom):
        for sonde in SONDES:
            try:
                res = sonde(s)
            except Exception:
                continue
            if res:
                return res
    return (None, '', '')


# ------------------------------------------------------------------

def sauver(rows):
    with open(CSV, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=rows[0].keys())
        w.writeheader()
        w.writerows(rows)


def test_une(nom):
    """python -u detect_ats.py --test Sanofi"""
    print(f'Test sur : {nom}\n')
    for sl in slugs(nom):
        print(f'  identifiant "{sl}"')
        for sonde in SONDES:
            try:
                res = sonde(sl)
            except Exception as e:
                print(f'    {sonde.__name__:24s} erreur {type(e).__name__}')
                continue
            etat = f'TROUVE  {res}' if res else '-'
            print(f'    {sonde.__name__:24s} {etat}')
            if res:
                return 0
    print('\nRien trouve.')
    return 1


def main():
    if len(sys.argv) > 2 and sys.argv[1] == '--test':
        return test_une(' '.join(sys.argv[2:]))

    with open(CSV, encoding='utf-8') as f:
        rows = list(csv.DictReader(f))

    todo = [r for r in rows if not r.get('ats') or r['ats'] in ('', 'inconnu')]
    print(f'{len(rows)} entreprises, {len(todo)} a sonder')
    print(f'{PARALLELISME} en parallele\n', flush=True)

    fait = 0
    with ThreadPoolExecutor(max_workers=PARALLELISME) as pool:
        futurs = {pool.submit(analyser, r['nom']): r for r in todo}
        for fut in as_completed(futurs):
            row = futurs[fut]
            fait += 1
            try:
                ats, tenant, url = fut.result()
            except Exception as e:
                print(f'  [{fait:3d}/{len(todo)}] ERREUR {row["nom"]} : {e}',
                      flush=True)
                continue

            row['ats'] = ats or 'inconnu'
            row['ats_tenant'] = tenant
            row['url_carrieres'] = url
            marque = 'OK ' if ats else '?? '
            print(f'  [{fait:3d}/{len(todo)}] {marque} {row["nom"][:36]:38s} '
                  f'{ats or "-":16s} {tenant}', flush=True)

            if fait % 10 == 0:
                sauver(rows)

    sauver(rows)

    print('\n--- Repartition ---', flush=True)
    compte = Counter(r['ats'] for r in rows)
    for ats, n in compte.most_common():
        print(f'  {n:4d}  {ats}')
    trouves = sum(1 for r in rows if r['ats'] != 'inconnu')
    print(f'\n{trouves}/{len(rows)} entreprises exploitables')

    # Garde-fou : un seul ATS ultra-dominant = faux positif quasi certain.
    # Aucun ATS ne depasse 35 % du marche francais des grands groupes.
    for ats, n in compte.most_common(1):
        if ats != 'inconnu' and n > 0.6 * len(rows):
            print(f'\n{"!"*60}')
            print(f'ALERTE : {ats} represente {100*n//len(rows)} % des resultats.')
            print('C\'est un FAUX POSITIF. Une API repond 200 pour des')
            print('societes inexistantes. Ne pas utiliser ce CSV.')
            print('!'*60)
            return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
