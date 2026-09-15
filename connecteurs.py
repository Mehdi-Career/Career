#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
connecteurs.py

Un connecteur par ATS. Chacun prend (tenant, url_carrieres) et renvoie
une liste d'Offre normalisees.

Cinq ATS exposent une API JSON publique, utilisee par leur propre front :
Workday, Greenhouse, Lever, SmartRecruiters, Ashby. C'est propre, rapide,
et ca ne declenche aucune protection anti-bot.

Les autres (SuccessFactors, Taleo, Avature, iCIMS, Cornerstone, Talentsoft)
passent par du HTML. Plus fragile, a surveiller.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import re
import time

import requests
from bs4 import BeautifulSoup

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/124.0 Safari/537.36')
HEADERS = {'User-Agent': UA, 'Accept-Language': 'fr-FR,fr;q=0.9'}
TIMEOUT = 20


@dataclass
class Offre:
    entreprise: str = ''
    titre: str = ''
    url: str = ''
    lieu: str = ''
    contrat: str = ''
    date_publication: str = ''      # ISO 8601, chaine vide si inconnue
    description: str = ''
    ats: str = ''
    id_externe: str = ''

    def cle(self):
        """Cle de deduplication."""
        t = re.sub(r'\s+', ' ', self.titre.lower().strip())
        return f'{self.entreprise.lower().strip()}|{t}'


def _get(url, **kw):
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, **kw)
        return r if r.status_code < 400 else None
    except requests.RequestException:
        return None


def _post(url, json_body):
    try:
        h = dict(HEADERS)
        h['Content-Type'] = 'application/json'
        r = requests.post(url, headers=h, json=json_body, timeout=TIMEOUT)
        return r if r.status_code < 400 else None
    except requests.RequestException:
        return None


def _texte(html):
    if not html:
        return ''
    return BeautifulSoup(html, 'html.parser').get_text(' ', strip=True)


# ==================================================================
# API JSON - fiables
# ==================================================================

def greenhouse(tenant, url_carrieres, max_offres=200):
    r = _get(f'https://boards-api.greenhouse.io/v1/boards/{tenant}/jobs?content=true')
    if not r:
        return []
    out = []
    for j in r.json().get('jobs', [])[:max_offres]:
        out.append(Offre(
            titre=j.get('title', ''),
            url=j.get('absolute_url', ''),
            lieu=(j.get('location') or {}).get('name', ''),
            date_publication=j.get('updated_at', ''),
            description=_texte(j.get('content', '')),
            ats='greenhouse',
            id_externe=str(j.get('id', '')),
        ))
    return out


def lever(tenant, url_carrieres, max_offres=200):
    r = _get(f'https://api.lever.co/v0/postings/{tenant}?mode=json')
    if not r:
        return []
    out = []
    for j in r.json()[:max_offres]:
        cats = j.get('categories') or {}
        ts = j.get('createdAt')
        date = (datetime.fromtimestamp(ts / 1000, tz=timezone.utc).isoformat()
                if ts else '')
        out.append(Offre(
            titre=j.get('text', ''),
            url=j.get('hostedUrl', ''),
            lieu=cats.get('location', ''),
            contrat=cats.get('commitment', ''),
            date_publication=date,
            description=_texte(j.get('description', '')) + ' ' +
                        _texte(j.get('additional', '')),
            ats='lever',
            id_externe=j.get('id', ''),
        ))
    return out


def smartrecruiters(tenant, url_carrieres, max_offres=200):
    out, offset = [], 0
    while len(out) < max_offres:
        r = _get('https://api.smartrecruiters.com/v1/companies/'
                 f'{tenant}/postings?limit=100&offset={offset}')
        if not r:
            break
        data = r.json()
        lot = data.get('content', [])
        if not lot:
            break
        for j in lot:
            loc = j.get('location') or {}
            out.append(Offre(
                titre=j.get('name', ''),
                url=j.get('ref', '') or
                    f"https://jobs.smartrecruiters.com/{tenant}/{j.get('id','')}",
                lieu=' '.join(filter(None, [loc.get('city'), loc.get('region'),
                                            loc.get('country')])),
                contrat=(j.get('typeOfEmployment') or {}).get('label', ''),
                date_publication=j.get('releasedDate', ''),
                ats='smartrecruiters',
                id_externe=j.get('id', ''),
            ))
        offset += len(lot)
        if offset >= data.get('totalFound', 0):
            break
        time.sleep(0.5)
    return out[:max_offres]


def ashby(tenant, url_carrieres, max_offres=200):
    r = _get(f'https://api.ashbyhq.com/posting-api/job-board/{tenant}'
             '?includeCompensation=true')
    if not r:
        return []
    out = []
    for j in r.json().get('jobs', [])[:max_offres]:
        out.append(Offre(
            titre=j.get('title', ''),
            url=j.get('jobUrl', ''),
            lieu=j.get('location', ''),
            contrat=j.get('employmentType', ''),
            date_publication=j.get('publishedAt', ''),
            description=_texte(j.get('descriptionHtml', '')),
            ats='ashby',
            id_externe=j.get('id', ''),
        ))
    return out


def workday(tenant, url_carrieres, max_offres=200):
    """
    Workday expose /wday/cxs/{tenant}/{site}/jobs en POST.
    Le nom du site varie (Careers, External, ...) : on le deduit de l'URL
    carrieres, avec repli sur les valeurs les plus courantes.
    """
    m = re.search(r'https?://([a-z0-9\-]+)\.(wd\d+)\.myworkdayjobs\.com/([^/?#]+)',
                  url_carrieres or '', re.I)
    if m:
        host, wd, site = m.group(1), m.group(2), m.group(3)
        candidats = [(host, wd, site)]
    else:
        candidats = [(tenant, wd, s)
                     for wd in ('wd1', 'wd3', 'wd5', 'wd103')
                     for s in ('External', 'Careers', 'careers', tenant)]

    for host, wd, site in candidats:
        base = f'https://{host}.{wd}.myworkdayjobs.com'
        out, offset = [], 0
        ok = False
        while len(out) < max_offres:
            r = _post(f'{base}/wday/cxs/{host}/{site}/jobs',
                      {'appliedFacets': {}, 'limit': 20,
                       'offset': offset, 'searchText': ''})
            if not r:
                break
            try:
                data = r.json()
            except ValueError:
                break
            lot = data.get('jobPostings', [])
            if not lot:
                break
            ok = True
            for j in lot:
                path = j.get('externalPath', '')
                out.append(Offre(
                    titre=j.get('title', ''),
                    url=f'{base}/{site}{path}',
                    lieu=j.get('locationsText', ''),
                    date_publication=j.get('postedOn', ''),
                    description=j.get('bulletFields', [''])[0] if j.get('bulletFields') else '',
                    ats='workday',
                    id_externe=path,
                ))
            offset += len(lot)
            if offset >= data.get('total', 0):
                break
            time.sleep(0.4)
        if ok:
            return out[:max_offres]
    return []


# ==================================================================
# HTML - plus fragiles, a surveiller
# ==================================================================

def _html_generique(url, ats, selecteurs, max_offres=200):
    r = _get(url)
    if not r:
        return []
    soup = BeautifulSoup(r.text, 'html.parser')
    out = []
    for sel in selecteurs:
        for n in soup.select(sel)[:max_offres]:
            titre = n.get_text(' ', strip=True)
            href = n.get('href') or (n.find('a') or {}).get('href', '')
            if not titre or len(titre) > 160:
                continue
            out.append(Offre(
                titre=titre,
                url=requests.compat.urljoin(r.url, href) if href else r.url,
                ats=ats,
            ))
        if out:
            break
    return out


def successfactors(tenant, url_carrieres, max_offres=200):
    return _html_generique(url_carrieres, 'successfactors',
                           ['a.jobTitle', 'a[href*="/job/"]',
                            '.jobTitle a', 'td.jobTitle a'], max_offres)


def taleo(tenant, url_carrieres, max_offres=200):
    return _html_generique(url_carrieres, 'taleo',
                           ['a[href*="jobdetail"]', 'a.titlelink',
                            '.oracletaleocwsv2-accordion-head-info a'], max_offres)


def avature(tenant, url_carrieres, max_offres=200):
    return _html_generique(url_carrieres, 'avature',
                           ['a.link--block', '.list__item a', 'a[href*="/JobDetail"]'],
                           max_offres)


def icims(tenant, url_carrieres, max_offres=200):
    return _html_generique(url_carrieres, 'icims',
                           ['a.iCIMS_Anchor', '.title a', 'a[href*="/jobs/"]'],
                           max_offres)


def cornerstone(tenant, url_carrieres, max_offres=200):
    return _html_generique(url_carrieres, 'cornerstone',
                           ['a[href*="/requisition/"]', '.jobtitle a'], max_offres)


def talentsoft(tenant, url_carrieres, max_offres=200):
    return _html_generique(url_carrieres, 'talentsoft',
                           ['a.ts-offer-card__title', 'a[href*="/offre-de-emploi/"]',
                            '.offer-title a'], max_offres)


# ==================================================================

CONNECTEURS = {
    'workday': workday,
    'greenhouse': greenhouse,
    'lever': lever,
    'smartrecruiters': smartrecruiters,
    'ashby': ashby,
    'successfactors': successfactors,
    'taleo': taleo,
    'avature': avature,
    'icims': icims,
    'cornerstone': cornerstone,
    'talentsoft': talentsoft,
}

FIABLES = {'workday', 'greenhouse', 'lever', 'smartrecruiters', 'ashby'}
