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
    if r and isinstance(r.json().get('jobs'), list):
        return 'greenhouse', s, f'https://job-boards.greenhouse.io/{s}'
    return None


def sonde_lever(s):
    r = _get(f'https://api.lever.co/v0/postings/{s}?mode=json', True)
    if r and isinstance(r.json(), list):
        return 'lever', s, f'https://jobs.lever.co/{s}'
    return None


def sonde_smartrecruiters(s):
    r = _get(f'https://api.smartrecruiters.com/v1/companies/{s}/postings?limit=1', True)
    if r and 'content' in r.json():
        return 'smartrecruiters', s, f'https://jobs.smartrecruiters.com/{s}'
    return None


def sonde_ashby(s):
    r = _get(f'https://api.ashbyhq.com/posting-api/job-board/{s}', True)
    if r and 'jobs' in r.json():
        return 'ashby', s, f'https://jobs.ashbyhq.com/{s}'
    return None


def sonde_workday(s):
    """
    Workday : on tape la racine du tenant. S'il existe, Workday redirige
    vers son site carrieres par defaut, et l'URL finale porte le nom du site.
    """
    for wd in ('wd3', 'wd1', 'wd5', 'wd103', 'wd2'):
        r = _get(f'https://{s}.{wd}.myworkdayjobs.com/')
        if not r:
            continue
        m = re.search(
            r'https?://([a-z0-9\-]+)\.' + wd +
            r'\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([^/?#]+)',
            r.url, re.I)
        if m:
            return 'workday', m.group(1).lower(), r.url
    return None


def sonde_icims(s):
    r = _get(f'https://careers-{s}.icims.com/jobs/search')
    if r and 'icims' in r.url.lower():
        return 'icims', s, r.url
    return None


def sonde_teamtailor(s):
    r = _get(f'https://{s}.teamtailor.com/jobs')
    if r and 'teamtailor' in r.url.lower():
        return 'teamtailor', s, r.url
    return None


def sonde_recruitee(s):
    r = _get(f'https://{s}.recruitee.com/api/offers/', True)
    if r and 'offers' in r.json():
        return 'recruitee', s, f'https://{s}.recruitee.com'
    return None


def sonde_workable(s):
    r = _get(f'https://apply.workable.com/api/v1/widget/accounts/{s}', True)
    if r:
        return 'workable', s, f'https://apply.workable.com/{s}'
    return None


# Ordre : les plus courants chez les grands groupes francais d'abord
SONDES = [
    sonde_workday,
    sonde_smartrecruiters,
    sonde_greenhouse,
    sonde_lever,
    sonde_ashby,
    sonde_icims,
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


def main():
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
    for ats, n in Counter(r['ats'] for r in rows).most_common():
        print(f'  {n:4d}  {ats}')
    trouves = sum(1 for r in rows if r['ats'] != 'inconnu')
    print(f'\n{trouves}/{len(rows)} entreprises exploitables')
    return 0


if __name__ == '__main__':
    sys.exit(main())
