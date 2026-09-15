#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
score.py

Reprend les offres au statut "a_scorer", applique le prompt de scoring,
et les classe selon le seuil.

  python score.py            # score tout ce qui attend
  python score.py --dry-run  # affiche sans ecrire en base

Cout : environ 0,004 EUR par offre (Haiku 4.5 + prompt caching).
"""

import argparse
import json
import os
import sqlite3
import sys
import time

import yaml
from anthropic import Anthropic

CONFIG = 'config.yaml'
RESERVOIR = 'reservoir-mehdi-koriche.yaml'
MODELE = 'claude-haiku-4-5-20251001'


# ------------------------------------------------------------------
# Prompt
# ------------------------------------------------------------------

SYSTEM = """Tu es un systeme de scoring de candidatures. Tu evalues des offres
d'emploi pour un candidat unique dont le profil complet t'est fourni.

La question que tu reponds n'est PAS "ce candidat correspond-il a cette offre ?"
mais "ce candidat peut-il ENTRER sur cette offre, une fois son CV adapte ?"

C'est une distinction centrale. Le CV sera reecrit pour chaque offre : on
reordonne, on requalifie, on remonte ce qui est enterre, on reprend le
vocabulaire exact de l'annonce. On n'invente jamais une experience ou un outil
jamais utilise.

---
PROFIL DU CANDIDAT
{reservoir}
---

DEUX NOTES A PRODUIRE

match_brut          Le CV actuel tel quel, ce que verrait un recruteur sans retouche.
match_atteignable   Apres reformulation legitime. C'est la note de filtrage.

GRILLE DU MATCH ATTEIGNABLE (total 100)

  couverture_directe        35   Competences deja detenues ET visibles sur le CV.
  couverture_atteignable    30   Competences reellement detenues mais a remonter,
                                 renommer ou reformuler pour apparaitre.
  compatibilite_seniorite   20   Sur-qualification ET sous-qualification penalisent.
                                 Reference : 7 ans d'experience affiches.
  fraicheur_annonce         15   Moins de 48 h = 15, degressif jusqu'a 0 a 30 jours.
                                 Si la date est inconnue, mets 8.

CLASSIFICATION DES ECARTS

ECART COMBLABLE (penalite faible, compte dans couverture_atteignable)
  - stack ou outil adjacent a ce que le candidat maitrise
  - meme competence designee par un autre terme
  - experience reelle mais releguee en fin de CV
  - outil present dans le reservoir mais absent du CV socle
  - taille de projet, budget ou portefeuille comparable
  - methodologie de vente que le candidat connait

MUR (eliminatoire, ne PAS penaliser en points, sortir l'offre)
  - nombre d'annees exige et verifiable, superieur a 7 ans
  - diplome precis impose que le candidat n'a pas
  - certification ou habilitation non detenue
  - anglais en clientele (voir regle dediee)
  - domaine metier technique : CVC, electricite, reseaux d'eau, genie civil,
    normes sectorielles, logiciels metier industriels, ERP metier specifique
  - experience sectorielle reglementee : pharma GxP, conformite bancaire
  - management d'equipe avec effectif chiffre exige
  - poste en ESN consistant a placer des consultants chez le client

ATTENTION : "serait un plus", "idealement", "apprecie", "serait un atout"
ne sont JAMAIS des murs. Ce sont des souhaits. Ne les traite pas comme des
exigences, cela eliminerait a tort des offres parfaitement accessibles.

REGLE ANGLAIS - trois etats, jamais binaire
  ok          Anglais interne, reporting, equipes etrangeres. Aucune penalite.
  mur         Anglais en clientele, negociation client, poste 100 % anglophone,
              perimetre EMEA ou Global. Eliminatoire.
  a_verifier  Ambigu, typiquement "French and English speaking" sans precision
              du perimetre. NE PAS REJETER. Scorer normalement et lever le
              drapeau anglais_a_verifier.

AUTRES DRAPEAUX (sans rejeter)
  management_exige       L'annonce demande d'encadrer une equipe.
  seniorite_limite       Ecart d'experience a la frontiere.
  teletravail_non_precise
  deplacements_frequents Preciser si hors IDF (redhibitoire) ou dans l'IDF (ok).

MOTS-CLES A EXTRAIRE
Remonte les termes de l'annonce a reprendre LITTERALEMENT dans le CV adapte.
Conserve la graphie exacte de l'annonce, sigles et accents compris
(si l'annonce ecrit "BtoB", ne renvoie pas "B2B").

Signale separement, dans outils_a_verifier, tout outil ou competence cite par
l'annonce dont le niveau du candidat n'est pas determinable depuis le profil.

SORTIE
Reponds UNIQUEMENT en JSON valide, sans preambule et sans balises Markdown.

{{
  "match_brut": 0,
  "match_atteignable": 0,
  "detail": {{"couverture_directe": 0, "couverture_atteignable": 0,
              "compatibilite_seniorite": 0, "fraicheur_annonce": 0}},
  "effort_adaptation": "leger",
  "justification": "Deux phrases maximum, factuelles.",
  "murs_detectes": [],
  "ecarts_comblables": [],
  "drapeaux": [],
  "mots_cles_a_integrer": [],
  "outils_a_verifier": [],
  "angle_de_candidature": "L'argument le plus fort, une phrase."
}}

CALIBRAGE
Sois severe sur les murs et genereux sur les ecarts comblables. Une offre avec
un seul mur reel sort, quel que soit le reste. Une offre sans mur ou le candidat
couvre la moitie des exigences doit passer : le CV sera adapte."""


def construire_prompt_offre(o):
    return f"""OFFRE A EVALUER

Entreprise : {o['entreprise']}
Secteur : {o['secteur']}
Intitule : {o['titre']}
Lieu : {o['lieu'] or 'non precise'}
Contrat : {o['contrat'] or 'non precise'}
Date de publication : {o['date_publication'] or 'inconnue'}
URL : {o['url']}

Description :
{(o['description'] or '(description non recuperee par le crawler)')[:12000]}"""


# ------------------------------------------------------------------

def extraire_json(txt):
    """Robuste aux balises Markdown et au texte parasite."""
    t = txt.strip()
    if t.startswith('```'):
        t = t.split('```')[1]
        if t.startswith('json'):
            t = t[4:]
    d, f = t.find('{'), t.rfind('}')
    if d == -1 or f == -1:
        raise ValueError('pas de JSON dans la reponse')
    return json.loads(t[d:f + 1])


def scorer(client, system_blocks, offre, tentatives=3):
    for essai in range(tentatives):
        try:
            r = client.messages.create(
                model=MODELE,
                max_tokens=1500,
                system=system_blocks,
                messages=[{'role': 'user',
                           'content': construire_prompt_offre(offre)}],
            )
            return extraire_json(r.content[0].text)
        except Exception as e:
            if essai == tentatives - 1:
                raise
            time.sleep(2 ** essai)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--limite', type=int, default=0)
    args = ap.parse_args()

    cle = os.environ.get('ANTHROPIC_API_KEY')
    if not cle:
        print('ANTHROPIC_API_KEY absente de l environnement.')
        return 1

    cfg = yaml.safe_load(open(CONFIG, encoding='utf-8'))
    seuil = cfg.get('scoring', {}).get('seuil', 55)
    seuil_push = cfg.get('scoring', {}).get('seuil_push', 80)
    reservoir = open(RESERVOIR, encoding='utf-8').read()

    # Le profil est identique a chaque appel : on le met en cache.
    # Les cache hits sont factures 10 % du prix d'entree.
    system_blocks = [{
        'type': 'text',
        'text': SYSTEM.format(reservoir=reservoir),
        'cache_control': {'type': 'ephemeral'},
    }]

    client = Anthropic(api_key=cle)
    cx = sqlite3.connect(cfg['base_de_donnees'])
    cx.row_factory = sqlite3.Row

    q = "SELECT * FROM offres WHERE statut = 'a_scorer' ORDER BY priorite, vue_le"
    if args.limite:
        q += f' LIMIT {args.limite}'
    offres = cx.execute(q).fetchall()

    if not offres:
        print('Rien a scorer.')
        return 0
    print(f'{len(offres)} offre(s) a scorer\n')

    retenues = rejetees = erreurs = 0

    for i, o in enumerate(offres, 1):
        try:
            res = scorer(client, system_blocks, o)
        except Exception as e:
            erreurs += 1
            print(f'  [{i:3d}] ERREUR {o["entreprise"]} / {o["titre"][:40]} : {e}')
            continue

        note = int(res.get('match_atteignable', 0))
        murs = res.get('murs_detectes') or []
        drapeaux = res.get('drapeaux') or []

        if murs:
            statut, marque = 'rejetee_mur', f'MUR  {murs[0][:45]}'
        elif note >= seuil_push:
            statut, marque = 'a_generer', 'PUSH'
        elif note >= seuil:
            statut, marque = 'a_generer', 'OK  '
        else:
            statut, marque = 'rejetee_score', 'bas '

        if statut == 'a_generer':
            retenues += 1
        else:
            rejetees += 1

        d = ' +' + ','.join(drapeaux) if drapeaux else ''
        print(f'  [{i:3d}] {marque} {note:3d}/100  '
              f'{o["entreprise"][:26]:28s} {o["titre"][:42]}{d}')

        if not args.dry_run:
            cx.execute("""UPDATE offres SET statut=?, match_brut=?,
                          match_atteignable=?, scoring_json=? WHERE cle=?""",
                       (statut, int(res.get('match_brut', 0)), note,
                        json.dumps(res, ensure_ascii=False), o['cle']))
            cx.commit()

    print(f'\n{"="*62}')
    print(f'  Retenues (>= {seuil})  : {retenues}   -> statut a_generer')
    print(f'  Rejetees             : {rejetees}')
    if erreurs:
        print(f'  Erreurs              : {erreurs}   (restent a_scorer)')
    print(f'  Cout estime          : ~{len(offres)*0.004:.2f} EUR')
    print('='*62)
    return 0


if __name__ == '__main__':
    sys.exit(main())
