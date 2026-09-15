#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
detect_ats.py

Remplit automatiquement url_carrieres, ats et ats_tenant dans entreprises-cibles.csv.

A lancer une fois au setup, puis tous les 6 mois (les groupes changent d'ATS).
Reprend ou il s'est arrete : relançable sans risque.

  pip install requests beautifulsoup4
  python detect_ats.py
"""

import csv, re, time, sys
from urllib.parse import urlparse, urljoin

import requests
from bs4 import BeautifulSoup

CSV = 'entreprises-cibles.csv'
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/124.0 Safari/537.36')
HEADERS = {'User-Agent': UA, 'Accept-Language': 'fr-FR,fr;q=0.9'}
TIMEOUT = 20
PAUSE = 1.5          # politesse : on ne martele pas les serveurs

# ------------------------------------------------------------------
# Signatures ATS. L'ordre compte : du plus specifique au plus generique.
# ------------------------------------------------------------------
SIGNATURES = [
    ('workday',        r'([a-z0-9\-]+)\.(?:wd\d+\.)?myworkdayjobs\.com'),
    ('workday',        r'/wday/cxs/([a-z0-9\-]+)/'),
    ('successfactors', r'([a-z0-9\-]+)\.(?:jobs\.)?successfactors\.(?:com|eu)'),
    ('successfactors', r'career(?:s)?\d*\.successfactors\.[a-z]+'),
    ('taleo',          r'([a-z0-9\-]+)\.taleo\.net'),
    ('avature',        r'([a-z0-9\-]+)\.avature\.net'),
    ('smartrecruiters', r'smartrecruiters\.com/([A-Za-z0-9\-]+)'),
    ('icims',          r'([a-z0-9\-]+)\.icims\.com'),
    ('cornerstone',    r'([a-z0-9\-]+)\.csod\.com'),
    ('greenhouse',     r'(?:job-)?boards(?:\.eu)?\.greenhouse\.io/([a-z0-9\-]+)'),
    ('greenhouse',     r'greenhouse\.io/embed/job_board\?for=([a-z0-9\-]+)'),
    ('lever',          r'jobs\.lever\.co/([a-z0-9\-]+)'),
    ('ashby',          r'jobs\.ashbyhq\.com/([a-z0-9\-]+)'),
    ('talentsoft',     r'([a-z0-9\-]+)\.(?:talentsoft|talent-soft)\.com'),
    ('teamtailor',     r'([a-z0-9\-]+)\.teamtailor\.com'),
    ('workable',       r'([a-z0-9\-]+)\.workable\.com'),
    ('recruitee',      r'([a-z0-9\-]+)\.recruitee\.com'),
    ('flatchr',        r'([a-z0-9\-]+)\.flatchr\.io'),
    ('beetween',       r'([a-z0-9\-]+)\.beetween\.com'),
    ('softgarden',     r'([a-z0-9\-]+)\.softgarden\.io'),
    ('wttj',           r'welcometothejungle\.com/fr/companies/([a-z0-9\-]+)'),
]

# Textes de liens qui menent vers un espace carrieres
CAREER_HINTS = re.compile(
    r'carri[eè]re|recrutement|nous\s*rejoindre|rejoignez|nos\s*offres|'
    r'emploi|job|career|talent|opportunit', re.I)


def http_get(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
        if r.status_code < 400:
            return r
    except requests.RequestException:
        pass
    return None


def detect_in(text):
    """Cherche une signature ATS dans un bloc de texte (HTML ou URL)."""
    for ats, pattern in SIGNATURES:
        m = re.search(pattern, text, re.I)
        if m:
            tenant = m.group(1) if m.groups() else ''
            return ats, tenant.lower()
    return '', ''


def find_careers_url(nom):
    """
    Trouve la page carrieres. Deux passes :
      1. DuckDuckGo HTML (pas de cle API necessaire)
      2. Motifs d'URL classiques sur le domaine trouve
    """
    q = requests.utils.quote(f'{nom} site carrieres recrutement France')
    r = http_get(f'https://html.duckduckgo.com/html/?q={q}')
    if not r:
        return ''

    soup = BeautifulSoup(r.text, 'html.parser')
    for a in soup.select('a.result__a')[:8]:
        href = a.get('href', '')
        m = re.search(r'uddg=([^&]+)', href)
        if m:
            href = requests.utils.unquote(m.group(1))
        if not href.startswith('http'):
            continue
        # Un lien qui pointe deja sur un ATS connu : jackpot
        ats, _ = detect_in(href)
        if ats:
            return href
        if CAREER_HINTS.search(href):
            return href
    return ''


def probe_common_paths(base):
    """Essaie les chemins carrieres classiques sur un domaine."""
    host = urlparse(base).netloc
    root = f'https://{host}'
    for path in ('/carrieres', '/fr/carrieres', '/nous-rejoindre', '/recrutement',
                 '/careers', '/fr/careers', '/jobs', '/emploi'):
        r = http_get(urljoin(root, path))
        if r:
            return r
    return http_get(root)


def analyse(nom):
    """Retourne (url_carrieres, ats, tenant)."""
    url = find_careers_url(nom)
    if not url:
        return '', '', ''

    # L'URL elle-meme porte parfois la signature
    ats, tenant = detect_in(url)
    if ats:
        return url, ats, tenant

    r = http_get(url) or probe_common_paths(url)
    if not r:
        return url, '', ''

    # L'URL finale apres redirections
    ats, tenant = detect_in(r.url)
    if ats:
        return r.url, ats, tenant

    # Puis le HTML : iframes, scripts, liens sortants
    ats, tenant = detect_in(r.text)
    if ats:
        return r.url, ats, tenant

    # Dernier essai : suivre le lien "carrieres" de la page
    soup = BeautifulSoup(r.text, 'html.parser')
    for a in soup.find_all('a', href=True):
        if CAREER_HINTS.search(a.get_text() or '') or CAREER_HINTS.search(a['href']):
            nxt = urljoin(r.url, a['href'])
            r2 = http_get(nxt)
            if r2:
                ats, tenant = detect_in(r2.url) or ('', '')
                if not ats:
                    ats, tenant = detect_in(r2.text)
                if ats:
                    return r2.url, ats, tenant
    return r.url, '', ''


def main():
    with open(CSV, encoding='utf-8') as f:
        rows = list(csv.DictReader(f))

    todo = [r for r in rows if not r['ats']]
    print(f'{len(rows)} entreprises, {len(todo)} a traiter\n')

    for i, row in enumerate(rows, 1):
        if row['ats']:
            continue
        nom = row['nom']
        try:
            url, ats, tenant = analyse(nom)
        except Exception as e:
            print(f'  [{i:3d}] {nom:40s} ERREUR {e}')
            continue

        row['url_carrieres'] = url
        row['ats'] = ats or 'inconnu'
        row['ats_tenant'] = tenant
        flag = 'OK ' if ats else '?? '
        print(f'  [{i:3d}] {flag} {nom:40s} {ats or "-":16s} {tenant}')

        # Sauvegarde a chaque ligne : relançable apres interruption
        with open(CSV, 'w', newline='', encoding='utf-8') as f:
            w = csv.DictWriter(f, fieldnames=rows[0].keys())
            w.writeheader()
            w.writerows(rows)

        time.sleep(PAUSE)

    from collections import Counter
    print('\n--- Repartition des ATS ---')
    for ats, n in Counter(r['ats'] for r in rows).most_common():
        print(f'  {n:4d}  {ats}')
    inconnus = [r['nom'] for r in rows if r['ats'] == 'inconnu']
    if inconnus:
        print(f'\n{len(inconnus)} a traiter a la main :')
        for n in inconnus[:30]:
            print(f'  - {n}')


if __name__ == '__main__':
    sys.exit(main())
