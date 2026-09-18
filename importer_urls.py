#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
importer_urls.py

Ajoute au CSV les entreprises dont l'ATS n'est PAS devinable
(Workday, Taleo, SuccessFactors, Avature, iCIMS, Cornerstone...).

POURQUOI CET OUTIL EXISTE
Greenhouse, Lever, Ashby et SmartRecruiters utilisent un identifiant
derive du nom de l'entreprise : on peut le deviner. Workday et les autres
non : le tenant est arbitraire, le centre de donnees varie (wd1, wd3, wd5),
le nom du site carrieres aussi. Aucun annuaire public n'existe.
La seule methode fiable est de partir de l'URL reelle.

COMMENT S'EN SERVIR
1. Ouvre urls-carrieres.txt
2. Une ligne par entreprise :   Nom ; URL de la page carrieres
3. Lance :  python -u importer_urls.py

Le script extrait tout seul l'ATS, le tenant et le site, puis met a jour
entreprises-cibles.csv.

OU TROUVER L'URL
Cherche "<nom de l'entreprise> carrieres" sur Google, ouvre la page des
offres, et copie ce qui est dans la barre d'adresse. Exemples valides :

  https://danone.wd3.myworkdayjobs.com/fr-FR/Danone_Careers
  https://jobs.smartrecruiters.com/Thales
  https://sncf.taleo.net/careersection/2/moresearch.ftl
  https://performancemanager.successfactors.eu/sfcareer/jobreqcareer?company=XYZ
  https://careers.orange.com/...
"""

import csv
import re
import sys
from urllib.parse import urlparse

CSV = 'entreprises-cibles.csv'
URLS = 'urls-carrieres.txt'

# (ats, motif, groupe_tenant, groupe_site)
MOTIFS = [
    ('workday',
     r'https?://([a-z0-9\-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([^/?#]+)',
     1, 3),
    ('smartrecruiters', r'smartrecruiters\.com/([A-Za-z0-9\-]+)', 1, None),
    ('greenhouse', r'(?:job-)?boards(?:\.eu)?\.greenhouse\.io/([a-z0-9\-]+)', 1, None),
    ('lever', r'jobs\.lever\.co/([a-z0-9\-]+)', 1, None),
    ('ashby', r'jobs\.ashbyhq\.com/([a-z0-9\-]+)', 1, None),
    ('taleo', r'https?://([a-z0-9\-]+)\.taleo\.net', 1, None),
    ('successfactors', r'company=([A-Za-z0-9]+)', 1, None),
    ('successfactors', r'([a-z0-9\-]+)\.(?:jobs\.)?successfactors\.(?:com|eu)', 1, None),
    ('avature', r'https?://([a-z0-9\-]+)\.avature\.net', 1, None),
    ('icims', r'https?://([a-z0-9\-]+)\.icims\.com', 1, None),
    ('cornerstone', r'https?://([a-z0-9\-]+)\.csod\.com', 1, None),
    ('talentsoft', r'https?://([a-z0-9\-]+)\.(?:talentsoft|talent-soft)\.com', 1, None),
    ('teamtailor', r'https?://([a-z0-9\-]+)\.teamtailor\.com', 1, None),
    ('recruitee', r'https?://([a-z0-9\-]+)\.recruitee\.com', 1, None),
    ('workable', r'apply\.workable\.com/([a-z0-9\-]+)', 1, None),
    ('flatchr', r'https?://([a-z0-9\-]+)\.flatchr\.io', 1, None),
    ('beetween', r'https?://([a-z0-9\-]+)\.beetween\.com', 1, None),
]


def analyser_url(url):
    """Renvoie (ats, tenant, site) ou (None, '', '')."""
    for ats, motif, gt, gs in MOTIFS:
        m = re.search(motif, url, re.I)
        if m:
            tenant = m.group(gt).lower()
            site = m.group(gs) if gs else ''
            # Workday : le tenant sert aussi de nom de site par defaut
            if ats == 'workday' and not site:
                site = tenant
            return ats, tenant, site
    return None, '', ''


def main():
    try:
        lignes = open(URLS, encoding='utf-8').read().splitlines()
    except FileNotFoundError:
        print(f'{URLS} introuvable. Cree-le avec une ligne par entreprise :')
        print('  Danone ; https://danone.wd3.myworkdayjobs.com/fr-FR/Danone_Careers')
        return 1

    with open(CSV, encoding='utf-8') as f:
        rows = list(csv.DictReader(f))
    index = {r['nom'].strip().lower(): r for r in rows}

    maj = ajout = ignore = echec = 0

    for ligne in lignes:
        ligne = ligne.strip()
        if not ligne or ligne.startswith('#'):
            continue
        if ';' not in ligne:
            print(f'  ! ligne sans point-virgule : {ligne[:60]}')
            echec += 1
            continue

        nom, url = [p.strip() for p in ligne.split(';', 1)]
        ats, tenant, site = analyser_url(url)

        if not ats:
            # Plateforme non reconnue : ce n'est PAS un echec. Le
            # connecteur universel part de l'URL seule et sait lire
            # les API JSON courantes, le JSON embarque, le schema.org
            # et les liens d'offres du HTML.
            hote = urlparse(url).netloc
            if not hote:
                print(f'  !! {nom:34s} URL invalide : {url[:50]}')
                echec += 1
                continue
            ats, tenant = 'generique', hote.split('.')[0]
            print(f'  ~~ {nom:34s} plateforme inconnue -> connecteur universel')

        row = index.get(nom.lower())
        if row:
            deja = row.get('ats', '')
            row['ats'] = ats
            row['ats_tenant'] = tenant
            row['url_carrieres'] = url
            if site and ats == 'workday':
                row['url_carrieres'] = url
            maj += 1
            marque = 'MAJ' if deja in ('', 'inconnu') else 'REMPLACE'
            print(f'  {marque:8s} {nom:34s} {ats:16s} {tenant}')
        else:
            nouveau = {k: '' for k in rows[0].keys()}
            nouveau.update({'nom': nom, 'secteur': 'Ajout manuel', 'priorite': '1',
                            'url_carrieres': url, 'ats': ats, 'ats_tenant': tenant,
                            'compte_cree': 'non', 'candidatures_mois': '0',
                            'actif': 'oui'})
            rows.append(nouveau)
            index[nom.lower()] = nouveau
            ajout += 1
            print(f'  AJOUT    {nom:34s} {ats:16s} {tenant}')

    with open(CSV, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=rows[0].keys())
        w.writeheader()
        w.writerows(rows)

    exploitables = sum(1 for r in rows if r.get('ats') not in ('', 'inconnu'))
    print(f'\n{maj} mise(s) a jour, {ajout} ajout(s), {echec} echec(s)')
    gen = sum(1 for r in rows if r.get('ats') == 'generique')
    if gen:
        print(f'{gen} entreprise(s) sur le connecteur universel')
    print(f'{exploitables}/{len(rows)} entreprises exploitables')
    return 0


if __name__ == '__main__':
    sys.exit(main())
