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
import json
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




# Un tenant qui vaut ca n'est pas un identifiant : c'est un sous-domaine
# generique capture par erreur. Dans ce cas, seule url_carrieres compte.
_TENANTS_NULS = {'www', 'app', 'tt', 'careers', 'career', 'jobs', 'job',
                 'files', 'events', 'emploi', 'emplois', 'carrieres',
                 'recrutement', 'talent', 'api', 'static', 'cdn', 'fr', 'en'}


def _base(url):
    """Schema + domaine d'une URL, sans le chemin."""
    u = (url or '').strip()
    if not u.startswith('http'):
        return ''
    return '/'.join(u.rstrip('/').split('/')[:3])


def _lieu_souple(j):
    """Le lieu se cache sous des cles tres variables selon l'ATS."""
    for k in ('location', 'city', 'ville', 'locationsText', 'place'):
        v = j.get(k)
        if isinstance(v, dict):
            v = v.get('name') or v.get('city') or v.get('label') or ''
        if isinstance(v, list):
            v = ', '.join(str(x.get('name') if isinstance(x, dict) else x)
                          for x in v[:3])
        if v:
            return str(v)
    return ''

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
    """
    Point d'entree universel de la plateforme carrieres SuccessFactors (RMK).
    Tout site carrieres SF expose cette API sur son PROPRE domaine :

        POST https://jobs.engie.com/services/recruiting/v1/jobs

    C'est la meme API que celle utilisee par leur barre de recherche.
    Propre, paginee, et sans aucune protection anti-bot.
    """
    base = (url_carrieres or '').rstrip('/')
    if not base.startswith('http'):
        return []
    # Ne garder que le schema + le domaine
    base = '/'.join(base.split('/')[:3])

    out, page = [], 0
    while len(out) < max_offres and page < 20:
        try:
            h = dict(HEADERS)
            h['Content-Type'] = 'application/json'
            r = requests.post(f'{base}/services/recruiting/v1/jobs', headers=h,
                              timeout=TIMEOUT,
                              json={'locale': 'fr_FR', 'pageNumber': page,
                                    'sortBy': '', 'keywords': '', 'location': '',
                                    'facetFilters': {}, 'brand': '', 'skills': [],
                                    'categoryId': 0, 'alertId': '',
                                    'rcmCandidateId': ''})
            if r.status_code >= 400:
                break
            d = r.json()
        except (requests.RequestException, ValueError):
            break

        lot = d.get('jobs') or d.get('data') or []
        if not lot:
            break

        for j in lot:
            jid = str(j.get('jobId') or j.get('id') or '')
            lieu = j.get('location') or j.get('city') or ''
            if isinstance(lieu, dict):
                lieu = lieu.get('name', '')
            out.append(Offre(
                titre=j.get('title') or j.get('jobTitle') or '',
                url=j.get('jobUrl') or j.get('applyUrl') or f'{base}/job/{jid}',
                lieu=str(lieu),
                contrat=str(j.get('employmentType') or j.get('jobType') or ''),
                date_publication=str(j.get('postedDate') or j.get('startDate') or ''),
                description=_texte(j.get('jobDescription') or j.get('description') or ''),
                ats='successfactors',
                id_externe=jid,
            ))

        total = d.get('totalJobs')
        if total is not None and len(out) >= int(total):
            break
        page += 1
        time.sleep(0.4)

    return out[:max_offres]


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

FIABLES = {'workday', 'greenhouse', 'lever', 'smartrecruiters', 'ashby',
           'successfactors'}   # API RMK sur le domaine du site carrieres

# ==================================================================
# NEXT.JS generique  -  sites carrieres maison
# Tout le contenu est dans <script id="__NEXT_DATA__"> en JSON.
# Fonctionne quelle que soit la plateforme sous-jacente.
# ==================================================================

_CLES_TITRE = ('title', 'jobtitle', 'name', 'intitule', 'libelle')
_CLES_LIEU = ('location', 'city', 'ville', 'addresslocality', 'lieu')
_CLES_CONTRAT = ('contracttype', 'contrat', 'employmenttype', 'jobtype')
_CLES_DATE = ('publicationdate', 'dateposted', 'datepublication', 'posteddate')


def _valeur(d, cles):
    for k in d:
        if k.lower() in cles:
            v = d[k]
            if isinstance(v, dict):
                v = v.get('name') or v.get('label') or v.get('addressLocality') or ''
            if isinstance(v, list):
                v = ', '.join(str(x) for x in v[:3])
            if isinstance(v, (str, int)):
                return str(v)
    return ''


def _collecter(noeud, out, prof=0):
    if prof > 12 or len(out) > 400:
        return
    if isinstance(noeud, dict):
        cles = {k.lower() for k in noeud}
        if any(c in cles for c in _CLES_TITRE) and \
           (any(c in cles for c in _CLES_LIEU) or
                any(c in cles for c in ('id', 'offerid', 'jobid'))):
            t = _valeur(noeud, _CLES_TITRE)
            if t and 3 < len(t) < 200:
                out.append(noeud)
        for v in noeud.values():
            _collecter(v, out, prof + 1)
    elif isinstance(noeud, list):
        for v in noeud[:200]:
            _collecter(v, out, prof + 1)


def nextdata(tenant, url_carrieres, max_offres=200):
    base = '/'.join((url_carrieres or '').rstrip('/').split('/')[:3])
    if not base.startswith('http'):
        return []

    vus, out = set(), []
    for chemin in ('/nos-offres', '/offres', '/jobs', '/emplois',
                   '/offres-emploi', '/search', '/'):
        for page in range(0, 6):
            sep = '&' if '?' in chemin else '?'
            url = f'{base}{chemin}' + (f'{sep}page={page}' if page else '')
            r = _get(url)
            if not r:
                break
            m = re.search(r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>',
                          r.text[:2000000], re.S)
            if not m:
                break
            try:
                data = json.loads(m.group(1))
            except ValueError:
                break

            brut = []
            _collecter(data, brut)
            nouveaux = 0
            for j in brut:
                titre = _valeur(j, _CLES_TITRE)
                jid = str(j.get('id') or j.get('offerId') or j.get('jobId') or '')
                cle = f'{titre}|{jid}'
                if not titre or cle in vus:
                    continue
                vus.add(cle)
                nouveaux += 1
                out.append(Offre(
                    titre=titre,
                    url=(j.get('externalApplyLink') or
                         (f'{base}/nos-offres/{jid}' if jid else r.url)),
                    lieu=_valeur(j, _CLES_LIEU),
                    contrat=_valeur(j, _CLES_CONTRAT),
                    date_publication=_valeur(j, _CLES_DATE),
                    description=_texte(j.get('description', ''))[:8000],
                    ats='nextdata',
                    id_externe=jid,
                ))
            if not nouveaux or len(out) >= max_offres:
                break
            time.sleep(0.4)
        if out:
            break
    return out[:max_offres]


# Talentry et Altays passent par le meme extracteur : leurs vitrines
# sont des sites Next.js.
talentry = nextdata
altays = nextdata


# Enregistrement apres definition des fonctions
CONNECTEURS.update({
    'nextdata': nextdata,
    'talentry': talentry,
    'altays': altays,
})
FIABLES.add('nextdata')

# ==================================================================
# TEAMTAILOR
# 16 entreprises detectees. Les tenants extraits sont souvent inutiles
# ("www", "app", "tt") parce que ces sites tournent sur un domaine
# maison : careers.payfit.com, career.studi.com, jobs.openclassrooms.com.
# => On part TOUJOURS de url_carrieres, jamais du tenant.
# Format de lien confirme sur carrieres.lefebvre-dalloz.fr :
#   /jobs/8395784-commercial-e-sedentaire-formation-cdd-h-f
# ==================================================================

def teamtailor(tenant, url_carrieres, max_offres=200):
    base = _base(url_carrieres) or (f'https://{tenant}.teamtailor.com'
                                    if tenant and tenant not in _TENANTS_NULS else '')
    if not base:
        return []

    # 1. Le flux JSON quand il est expose
    for chemin in ('/jobs.json', '/api/jobs'):
        r = _get(base + chemin)
        if r:
            try:
                d = r.json()
            except ValueError:
                continue
            lot = d if isinstance(d, list) else (d.get('jobs') or d.get('data') or [])
            if lot:
                out = []
                for j in lot[:max_offres]:
                    if not isinstance(j, dict):
                        continue
                    out.append(Offre(
                        titre=str(j.get('title') or j.get('name') or ''),
                        url=str(j.get('url') or j.get('careers_url') or base),
                        lieu=_lieu_souple(j),
                        contrat=str(j.get('employment_type') or j.get('contract') or ''),
                        date_publication=str(j.get('created_at') or j.get('published_at') or ''),
                        description=_texte(str(j.get('body') or j.get('description') or '')),
                        ats='teamtailor',
                        id_externe=str(j.get('id') or ''),
                    ))
                if out:
                    return out

    # 2. Repli HTML : les liens d'offres portent un identifiant numerique
    out, vus = [], set()
    for chemin in ('/jobs', '/fr/jobs', '/'):
        r = _get(base + chemin)
        if not r:
            continue
        for m in re.finditer(r'href="([^"]*?/jobs/(\d+)-([^"?#]*))"', r.text[:1500000]):
            jid = m.group(2)
            if jid in vus:
                continue
            vus.add(jid)
            titre = m.group(3).replace('-', ' ').strip()
            titre = re.sub(r'\s+', ' ', titre).capitalize()
            out.append(Offre(titre=titre,
                             url=requests.compat.urljoin(r.url, m.group(1)),
                             ats='teamtailor', id_externe=jid))
            if len(out) >= max_offres:
                break
        if out:
            break
    return out


# ==================================================================
# WORKABLE  -  API widget publique
# ==================================================================

def workable(tenant, url_carrieres, max_offres=200):
    if not tenant or tenant in _TENANTS_NULS:
        m = re.search(r'apply\.workable\.com/([a-z0-9\-]+)', url_carrieres or '', re.I)
        tenant = m.group(1) if m else ''
    if not tenant:
        return []

    r = _get(f'https://apply.workable.com/api/v1/widget/accounts/{tenant}?details=true')
    if not r:
        return []
    try:
        d = r.json()
    except ValueError:
        return []

    out = []
    for j in (d.get('jobs') or [])[:max_offres]:
        if not isinstance(j, dict):
            continue
        out.append(Offre(
            titre=str(j.get('title') or ''),
            url=str(j.get('url') or j.get('application_url')
                    or f"https://apply.workable.com/{tenant}/j/{j.get('shortcode', '')}"),
            lieu=_lieu_souple(j),
            contrat=str(j.get('employment_type') or j.get('type') or ''),
            date_publication=str(j.get('published_on') or j.get('created_at') or ''),
            description=_texte(str(j.get('description') or '')),
            ats='workable',
            id_externe=str(j.get('shortcode') or j.get('id') or ''),
        ))
    return out


# ==================================================================
# RECRUITEE  -  API publique
# ==================================================================

def recruitee(tenant, url_carrieres, max_offres=200):
    bases = []
    if tenant and tenant not in _TENANTS_NULS:
        bases.append(f'https://{tenant}.recruitee.com')
    b = _base(url_carrieres)
    if b and b not in bases:
        bases.append(b)

    for base in bases:
        r = _get(f'{base}/api/offers/')
        if not r:
            continue
        try:
            offres = r.json().get('offers') or []
        except ValueError:
            continue
        if not offres:
            continue
        out = []
        for j in offres[:max_offres]:
            if not isinstance(j, dict):
                continue
            lieu = ', '.join(x for x in (j.get('city'), j.get('country')) if x)
            out.append(Offre(
                titre=str(j.get('title') or ''),
                url=str(j.get('careers_url') or j.get('careers_apply_url') or base),
                lieu=lieu or _lieu_souple(j),
                contrat=str(j.get('employment_type_code') or j.get('options_cv') or ''),
                date_publication=str(j.get('published_at') or j.get('created_at') or ''),
                description=_texte(str(j.get('description') or '')),
                ats='recruitee',
                id_externe=str(j.get('id') or ''),
            ))
        if out:
            return out
    return []


# ==================================================================
# TALEEZ  -  API publique. Confirme : orsys.taleez.com
# ==================================================================

def taleez(tenant, url_carrieres, max_offres=200):
    base = _base(url_carrieres) or (f'https://{tenant}.taleez.com'
                                    if tenant and tenant not in _TENANTS_NULS else '')
    if not base:
        return []
    for chemin in ('/api/public/jobs', '/api/jobs', '/jobs.json'):
        r = _get(base + chemin)
        if not r:
            continue
        try:
            d = r.json()
        except ValueError:
            continue
        lot = d if isinstance(d, list) else (d.get('list') or d.get('jobs') or d.get('data') or [])
        if not lot:
            continue
        out = []
        for j in lot[:max_offres]:
            if not isinstance(j, dict):
                continue
            out.append(Offre(
                titre=str(j.get('label') or j.get('title') or ''),
                url=str(j.get('url') or f"{base}/fr/job/{j.get('id', '')}"),
                lieu=_lieu_souple(j),
                contrat=str(j.get('contract') or j.get('contractType') or ''),
                date_publication=str(j.get('dateCreation') or j.get('created_at') or ''),
                description=_texte(str(j.get('jobDescription') or j.get('description') or '')),
                ats='taleez',
                id_externe=str(j.get('id') or ''),
            ))
        if out:
            return out
    return []


# ==================================================================
# DIGITALRECRUITERS  -  Cegid, Paprec, Synergie
# Sites en Nuxt : le contenu vit dans window.__NUXT__, pas dans le HTML.
# ==================================================================

def digitalrecruiters(tenant, url_carrieres, max_offres=200):
    base = _base(url_carrieres)
    if not base:
        return []
    for chemin in ('/api/v1/jobs', '/api/jobs', '/wp-json/dr/v1/jobs'):
        r = _get(base + chemin)
        if not r:
            continue
        try:
            d = r.json()
        except ValueError:
            continue
        lot = d if isinstance(d, list) else (d.get('jobs') or d.get('data')
                                             or d.get('results') or [])
        if not lot:
            continue
        out = []
        for j in lot[:max_offres]:
            if not isinstance(j, dict):
                continue
            out.append(Offre(
                titre=str(j.get('title') or j.get('name') or ''),
                url=str(j.get('url') or j.get('link') or base),
                lieu=_lieu_souple(j),
                contrat=str(j.get('contract') or j.get('contract_type') or ''),
                date_publication=str(j.get('published_at') or j.get('date') or ''),
                description=_texte(str(j.get('description') or '')),
                ats='digitalrecruiters',
                id_externe=str(j.get('id') or j.get('reference') or ''),
            ))
        if out:
            return out
    return nuxt(tenant, url_carrieres, max_offres)


# ==================================================================
# NUXT generique  -  equivalent de nextdata pour les sites Nuxt
# ==================================================================

def nuxt(tenant, url_carrieres, max_offres=200):
    base = _base(url_carrieres)
    if not base:
        return []
    for chemin in ('/nos-offres', '/offres', '/jobs', '/fr', '/'):
        r = _get(base + chemin)
        if not r:
            continue
        m = re.search(r'window\.__NUXT__\s*=\s*(\{.*?\});?\s*</script>',
                      r.text[:2000000], re.S)
        if not m:
            continue
        try:
            data = json.loads(m.group(1))
        except ValueError:
            continue          # Nuxt serialise parfois en fonction JS, pas en JSON
        brut = []
        _collecter(data, brut)
        out, vus = [], set()
        for j in brut:
            titre = _valeur(j, _CLES_TITRE)
            jid = str(j.get('id') or j.get('reference') or '')
            cle = f'{titre}|{jid}'
            if not titre or cle in vus:
                continue
            vus.add(cle)
            out.append(Offre(titre=titre,
                             url=str(j.get('url') or j.get('link') or r.url),
                             lieu=_valeur(j, _CLES_LIEU),
                             contrat=_valeur(j, _CLES_CONTRAT),
                             date_publication=_valeur(j, _CLES_DATE),
                             description=_texte(str(j.get('description', '')))[:8000],
                             ats='nuxt', id_externe=jid))
        if out:
            return out[:max_offres]
    return []


def flatchr(tenant, url_carrieres, max_offres=200):
    base = _base(url_carrieres) or (f'https://{tenant}.flatchr.io'
                                    if tenant and tenant not in _TENANTS_NULS else '')
    if not base:
        return []
    for chemin in ('/api/vacancy', '/vacancies.json', '/api/v1/vacancies'):
        r = _get(base + chemin)
        if not r:
            continue
        try:
            d = r.json()
        except ValueError:
            continue
        lot = d if isinstance(d, list) else (d.get('vacancies') or d.get('data') or [])
        if not lot:
            continue
        return [Offre(titre=str(j.get('title') or j.get('name') or ''),
                      url=str(j.get('url') or base),
                      lieu=_lieu_souple(j),
                      description=_texte(str(j.get('description') or '')),
                      ats='flatchr', id_externe=str(j.get('id') or ''))
                for j in lot[:max_offres] if isinstance(j, dict)]
    return []


CONNECTEURS.update({
    'teamtailor': teamtailor,
    'workable': workable,
    'recruitee': recruitee,
    'taleez': taleez,
    'digitalrecruiters': digitalrecruiters,
    'nuxt': nuxt,
    'flatchr': flatchr,
})
FIABLES.update({'teamtailor', 'workable', 'recruitee', 'taleez'})

# ==================================================================
# CONNECTEUR UNIVERSEL
#
# Marche a partir d'une simple URL de site carrieres, SANS savoir quelle
# plateforme est derriere. C'est le filet de securite : une entreprise
# dont on a l'URL n'est jamais perdue, meme si son ATS est inconnu.
#
# Quatre strategies, de la plus propre a la plus brutale :
#   1. les API JSON connues, testees sur le domaine du site
#   2. le JSON embarque dans la page (Next.js, Nuxt, JSON-LD)
#   3. les donnees structurees schema.org/JobPosting
#   4. les liens d'offres dans le HTML, reconnus a leur forme
# ==================================================================

# Points d'entree JSON exposes par les plateformes courantes, tous
# relatifs au domaine du site carrieres.
_API_CANDIDATES = (
    '/services/recruiting/v1/jobs',      # SuccessFactors RMK (POST)
    '/api/offers/',                      # Recruitee
    '/api/public/jobs',                  # Taleez
    '/api/v1/jobs', '/api/jobs',         # DigitalRecruiters et divers
    '/jobs.json',                        # Teamtailor et divers
    '/api/v1/search/jobs', '/api/search/jobs',
    '/wp-json/wp/v2/jobs',               # WordPress
)

# Un lien d'offre porte presque toujours un identifiant numerique
# apres un segment qui dit "offre".
_LIEN_OFFRE = re.compile(
    r'href=["\']([^"\']*?/(?:jobs?|offres?|offer|emplois?|poste|vacancy|'
    r'vacature|stelle|careers?|recrutement|job-detail|jobdetail|'
    r'nos-offres|offre-emploi)[/\-][^"\']*?(\d{3,})[^"\']*)["\']',
    re.I)

# Titre lisible dans le texte du lien, ou a defaut dans son URL
_TEXTE_LIEN = re.compile(r'>([^<>]{8,140})<')


def _offres_depuis_jsonld(html):
    """schema.org/JobPosting : balise <script type="application/ld+json">."""
    out = []
    for m in re.finditer(
            r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            html or '', re.S | re.I):
        try:
            d = json.loads(m.group(1).strip())
        except ValueError:
            continue
        for bloc in (d if isinstance(d, list) else [d]):
            if not isinstance(bloc, dict):
                continue
            if bloc.get('@type') == 'JobPosting':
                out.append(bloc)
            for k in ('itemListElement', '@graph'):
                for e in (bloc.get(k) or []):
                    e = e.get('item', e) if isinstance(e, dict) else e
                    if isinstance(e, dict) and e.get('@type') == 'JobPosting':
                        out.append(e)
    return out


def _lieu_jsonld(j):
    loc = j.get('jobLocation') or {}
    if isinstance(loc, list):
        loc = loc[0] if loc else {}
    adr = (loc or {}).get('address') or {}
    if isinstance(adr, str):
        return adr
    return ' '.join(str(adr.get(k, '')) for k in
                    ('addressLocality', 'addressRegion')).strip()


def generique(tenant, url_carrieres, max_offres=200):
    base = _base(url_carrieres)
    if not base:
        return []

    # --- 1. Les API JSON connues ---
    for chemin in _API_CANDIDATES:
        d = None
        if chemin.endswith('/jobs') and 'recruiting' in chemin:
            d = _post(base + chemin,
                      {'locale': 'fr_FR', 'pageNumber': 0, 'sortBy': '',
                       'keywords': '', 'location': '', 'facetFilters': {},
                       'brand': '', 'skills': [], 'categoryId': 0,
                       'alertId': '', 'rcmCandidateId': ''})
        else:
            r = _get(base + chemin)
            if r:
                try:
                    d = r.json()
                except ValueError:
                    d = None
        if not d:
            continue
        lot = d if isinstance(d, list) else (
            d.get('jobs') or d.get('offers') or d.get('data')
            or d.get('list') or d.get('results') or d.get('content') or [])
        lot = [j for j in lot if isinstance(j, dict)]
        if len(lot) < 2:
            continue
        out = []
        for j in lot[:max_offres]:
            titre = _valeur(j, _CLES_TITRE) or str(j.get('label') or '')
            if not titre:
                continue
            out.append(Offre(
                titre=titre,
                url=str(j.get('url') or j.get('careers_url') or j.get('link')
                        or j.get('jobUrl') or base),
                lieu=_lieu_souple(j), contrat=_valeur(j, _CLES_CONTRAT),
                date_publication=_valeur(j, _CLES_DATE),
                description=_texte(str(j.get('description')
                                       or j.get('jobDescription') or ''))[:8000],
                ats='generique',
                id_externe=str(j.get('id') or j.get('reference') or ''),
            ))
        if len(out) >= 2:
            return out

    # --- 2, 3, 4 : on charge les pages de liste ---
    for chemin in ('/nos-offres', '/offres', '/jobs', '/emplois',
                   '/offres-emploi', '/recherche', '/search', '/fr', ''):
        r = _get(base + chemin)
        if not r:
            continue
        html = r.text[:2000000]

        # 2. JSON embarque : Next.js puis Nuxt
        for motif in (r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>',
                      r'window\.__NUXT__\s*=\s*(\{.*?\});?\s*</script>'):
            m = re.search(motif, html, re.S)
            if not m:
                continue
            try:
                data = json.loads(m.group(1))
            except ValueError:
                continue
            brut = []
            _collecter(data, brut)
            out, vus = [], set()
            for j in brut:
                titre = _valeur(j, _CLES_TITRE)
                jid = str(j.get('id') or j.get('reference') or '')
                if not titre or f'{titre}|{jid}' in vus:
                    continue
                vus.add(f'{titre}|{jid}')
                out.append(Offre(
                    titre=titre,
                    url=str(j.get('url') or j.get('externalApplyLink')
                            or j.get('link') or r.url),
                    lieu=_valeur(j, _CLES_LIEU), contrat=_valeur(j, _CLES_CONTRAT),
                    date_publication=_valeur(j, _CLES_DATE),
                    description=_texte(str(j.get('description', '')))[:8000],
                    ats='generique', id_externe=jid))
            if len(out) >= 2:
                return out[:max_offres]

        # 3. schema.org/JobPosting
        jsonld = _offres_depuis_jsonld(html)
        if len(jsonld) >= 2:
            return [Offre(titre=str(j.get('title', '')),
                          url=str(j.get('url') or j.get('sameAs') or r.url),
                          lieu=_lieu_jsonld(j),
                          contrat=str(j.get('employmentType') or ''),
                          date_publication=str(j.get('datePosted') or ''),
                          description=_texte(str(j.get('description', '')))[:8000],
                          ats='generique',
                          id_externe=str(j.get('identifier', '') or ''))
                    for j in jsonld[:max_offres]]

        # 4. Liens d'offres dans le HTML
        out, vus = [], set()
        for m in _LIEN_OFFRE.finditer(html):
            lien, jid = m.group(1), m.group(2)
            if jid in vus:
                continue
            vus.add(jid)
            # Titre : le texte du lien, sinon le slug de l'URL
            fin = html[m.end():m.end() + 400]
            t = _TEXTE_LIEN.search(fin)
            titre = re.sub(r'<[^>]+>|\s+', ' ', t.group(1)).strip() if t else ''
            if not titre or len(titre) < 6:
                slug = re.sub(r'[/\-_]+', ' ', lien.split('/')[-1])
                titre = re.sub(r'\d{3,}|\.html?$', ' ', slug).strip().capitalize()
            if not titre or len(titre) < 6:
                continue
            out.append(Offre(titre=titre[:180],
                             url=requests.compat.urljoin(r.url, lien),
                             ats='generique', id_externe=jid))
            if len(out) >= max_offres:
                break
        if len(out) >= 2:
            return out
    return []


CONNECTEURS['generique'] = generique
CONNECTEURS['inconnu_avec_url'] = generique
