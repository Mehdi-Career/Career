#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
crawl.py

Interroge les ATS de toutes les entreprises de la liste, filtre, deduplique
et stocke les nouvelles offres en base avec le statut "a_scorer".

  pip install requests beautifulsoup4 pyyaml
  python crawl.py                 # crawl complet
  python crawl.py --priorite 1    # seulement la priorite 1
  python crawl.py --test Danone   # une seule entreprise, sans ecriture en base

Le scoring est fait ensuite par score.py.
"""

import argparse
import csv
import re
import sqlite3
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone

import yaml

from connecteurs import CONNECTEURS, FIABLES, Offre

CSV_ENTREPRISES = 'entreprises-cibles.csv'
CONFIG = 'config.yaml'


# ------------------------------------------------------------------
# Utilitaires
# ------------------------------------------------------------------

def sans_accents(s):
    """Normalise pour comparer : minuscules, sans accents, espaces tasses."""
    s = unicodedata.normalize('NFD', s or '')
    s = ''.join(c for c in s if unicodedata.category(c) != 'Mn')
    return re.sub(r'\s+', ' ', s.lower()).strip()


def parse_date(s):
    """Tolerant : ISO, 'Posted 3 Days Ago', 'Posted Today'."""
    if not s:
        return None
    s = s.strip()

    m = re.search(r'(\d+)\+?\s*day', s, re.I)
    if m:
        return datetime.now(timezone.utc) - timedelta(days=int(m.group(1)))
    if re.search(r'today|aujourd', s, re.I):
        return datetime.now(timezone.utc)
    if re.search(r'yesterday|hier', s, re.I):
        return datetime.now(timezone.utc) - timedelta(days=1)
    m = re.search(r'(\d+)\+?\s*month', s, re.I)
    if m:
        return datetime.now(timezone.utc) - timedelta(days=30 * int(m.group(1)))

    for fmt in ('%Y-%m-%dT%H:%M:%S%z', '%Y-%m-%dT%H:%M:%SZ',
                '%Y-%m-%dT%H:%M:%S.%fZ', '%Y-%m-%d', '%d/%m/%Y'):
        try:
            d = datetime.strptime(s[:len(fmt) + 8], fmt)
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


# ------------------------------------------------------------------
# Base
# ------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS offres (
  cle               TEXT PRIMARY KEY,
  entreprise        TEXT NOT NULL,
  secteur           TEXT,
  priorite          INTEGER,
  titre             TEXT NOT NULL,
  url               TEXT,
  lieu              TEXT,
  contrat           TEXT,
  date_publication  TEXT,
  description       TEXT,
  ats               TEXT,
  id_externe        TEXT,
  vue_le            TEXT NOT NULL,
  statut            TEXT NOT NULL DEFAULT 'a_scorer',
  match_brut        INTEGER,
  match_atteignable INTEGER,
  scoring_json      TEXT
);
CREATE INDEX IF NOT EXISTS idx_statut ON offres(statut);
CREATE INDEX IF NOT EXISTS idx_entreprise ON offres(entreprise);

CREATE TABLE IF NOT EXISTS journal_crawl (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  horodatage  TEXT,
  entreprise  TEXT,
  ats         TEXT,
  brutes      INTEGER,
  retenues    INTEGER,
  nouvelles   INTEGER,
  erreur      TEXT
);
"""


def ouvrir_base(chemin):
    cx = sqlite3.connect(chemin)
    cx.executescript(SCHEMA)
    cx.commit()
    return cx


# ------------------------------------------------------------------
# Filtres
# ------------------------------------------------------------------

class Filtres:
    def __init__(self, cfg):
        f = cfg['filtres']
        self.intitules = [sans_accents(x) for x in f['intitules']]
        self.intitules_exclus = [sans_accents(x) for x in f['intitules_exclus']]
        self.lieux = [sans_accents(x) for x in f['lieux']]
        self.contrats = [sans_accents(x) for x in f['contrats']]
        self.contrats_exclus = [sans_accents(x) for x in f['contrats_exclus']]
        self.age_max = f['anciennete_max_jours']

    def verdict(self, o: Offre):
        """Renvoie (garde: bool, raison: str)."""
        t = sans_accents(o.titre)
        if not t:
            return False, 'titre vide'

        for x in self.intitules_exclus:
            if x in t:
                return False, f'intitule exclu ({x})'

        if not any(x in t for x in self.intitules):
            return False, 'intitule hors cible'

        # Lieu : si le champ est vide, on laisse passer. Le scorer tranchera
        # sur la description. Rejeter sur un champ absent couterait trop cher.
        if o.lieu:
            if not any(x in sans_accents(o.lieu) for x in self.lieux):
                return False, f'hors IDF ({o.lieu})'

        if o.contrat:
            c = sans_accents(o.contrat)
            for x in self.contrats_exclus:
                if x in c:
                    return False, f'contrat exclu ({o.contrat})'

        d = parse_date(o.date_publication)
        if d:
            age = (datetime.now(timezone.utc) - d).days
            if age > self.age_max:
                return False, f'annonce de {age} jours'

        return True, ''


# ------------------------------------------------------------------
# Crawl
# ------------------------------------------------------------------

def charger_entreprises(priorite=None, nom=None):
    with open(CSV_ENTREPRISES, encoding='utf-8') as f:
        rows = list(csv.DictReader(f))
    rows = [r for r in rows if r.get('actif', 'oui') == 'oui']
    rows = [r for r in rows if r.get('ats') and r['ats'] != 'inconnu']
    if priorite:
        rows = [r for r in rows if r.get('priorite') == str(priorite)]
    if nom:
        n = sans_accents(nom)
        rows = [r for r in rows if n in sans_accents(r['nom'])]
    return rows


def crawler_entreprise(row, filtres, pause, max_offres):
    ats = row['ats']
    fn = CONNECTEURS.get(ats)
    if not fn:
        return [], 0, f'connecteur absent pour {ats}'

    try:
        brutes = fn(row.get('ats_tenant', ''), row.get('url_carrieres', ''),
                    max_offres)
    except Exception as e:
        return [], 0, f'{type(e).__name__}: {e}'

    retenues = []
    for o in brutes:
        o.entreprise = row['nom']
        garde, _ = filtres.verdict(o)
        if garde:
            retenues.append(o)

    time.sleep(pause)
    return retenues, len(brutes), ''


def enregistrer(cx, offres, row):
    """Insere les offres inconnues. Renvoie la liste des nouvelles."""
    maintenant = datetime.now(timezone.utc).isoformat()
    nouvelles = []
    for o in offres:
        cur = cx.execute('SELECT 1 FROM offres WHERE cle = ?', (o.cle(),))
        if cur.fetchone():
            continue
        cx.execute("""
            INSERT INTO offres (cle, entreprise, secteur, priorite, titre, url,
                                lieu, contrat, date_publication, description,
                                ats, id_externe, vue_le, statut)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?, 'a_scorer')
        """, (o.cle(), o.entreprise, row.get('secteur', ''),
              int(row.get('priorite') or 3), o.titre, o.url, o.lieu, o.contrat,
              o.date_publication, o.description[:8000], o.ats, o.id_externe,
              maintenant))
        nouvelles.append(o)
    cx.commit()
    return nouvelles


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--priorite', type=int, choices=[1, 2, 3])
    ap.add_argument('--test', metavar='ENTREPRISE',
                    help='une seule entreprise, sans ecriture en base')
    args = ap.parse_args()

    cfg = yaml.safe_load(open(CONFIG, encoding='utf-8'))
    filtres = Filtres(cfg)
    pause = cfg['crawl']['pause_entre_requetes']
    max_offres = cfg['crawl']['offres_max_par_entreprise']

    entreprises = charger_entreprises(args.priorite, args.test)
    if not entreprises:
        print('Aucune entreprise a crawler. As-tu lance detect_ats.py ?')
        return 1

    cx = None if args.test else ouvrir_base(cfg['base_de_donnees'])
    print(f'{len(entreprises)} entreprises\n')

    total_brutes = total_retenues = total_nouvelles = 0
    en_echec = []

    for i, row in enumerate(entreprises, 1):
        retenues, n_brutes, err = crawler_entreprise(row, filtres, pause, max_offres)
        total_brutes += n_brutes
        total_retenues += len(retenues)

        if err:
            en_echec.append((row['nom'], row['ats'], err))
            fiable = row['ats'] in FIABLES
            print(f"  [{i:3d}] {'!!' if fiable else ' !'} {row['nom']:38s} {err[:60]}")
            continue

        nouvelles = []
        if cx:
            nouvelles = enregistrer(cx, retenues, row)
            total_nouvelles += len(nouvelles)
            cx.execute("""INSERT INTO journal_crawl
                          (horodatage, entreprise, ats, brutes, retenues, nouvelles, erreur)
                          VALUES (?,?,?,?,?,?,'')""",
                       (datetime.now(timezone.utc).isoformat(), row['nom'],
                        row['ats'], n_brutes, len(retenues), len(nouvelles)))
            cx.commit()

        if retenues:
            marque = f'{len(nouvelles)} nouvelle(s)' if cx else 'test'
            print(f"  [{i:3d}] OK {row['nom']:38s} "
                  f"{n_brutes:4d} brutes -> {len(retenues):2d} retenues, {marque}")
            for o in retenues[:5]:
                print(f'          . {o.titre[:70]}  [{o.lieu[:28]}]')

    print(f'\n{"="*62}')
    print(f'  Offres brutes      : {total_brutes}')
    print(f'  Retenues (filtres) : {total_retenues}')
    if cx:
        print(f'  Nouvelles en base  : {total_nouvelles}  -> statut a_scorer')
    if en_echec:
        graves = [e for e in en_echec if e[1] in FIABLES]
        print(f'  En echec           : {len(en_echec)}'
              f'  (dont {len(graves)} sur ATS fiables, a corriger)')
    print('='*62)
    return 0


if __name__ == '__main__':
    sys.exit(main())
