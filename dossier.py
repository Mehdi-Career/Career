#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dossier.py

Pour chaque offre au statut "a_generer" : produit le CV adapte (.docx + .pdf)
et la lettre, puis passe l'offre en "a_envoyer".

  python dossier.py
  python dossier.py --limite 3

Cout : environ 0,05 EUR par dossier (Sonnet 5 + prompt caching).
"""

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import unicodedata
from pathlib import Path

import yaml
from anthropic import Anthropic

import cv_builder

CONFIG = 'config.yaml'
RESERVOIR = 'reservoir-mehdi-koriche.yaml'
MODELE = 'claude-sonnet-4-6'
SORTIE = Path('dossiers')


SYSTEM = """Tu adaptes un CV et rediges une lettre de motivation pour un candidat
unique, en fonction d'une offre d'emploi precise.

---
PROFIL COMPLET DU CANDIDAT (le reservoir)
{reservoir}
---

REGLES ABSOLUES

AUTORISE
  - reformuler, reordonner, requalifier ce qui a ete reellement fait
  - remonter une competence enterree en fin de CV
  - reprendre LITTERALEMENT le vocabulaire de l'annonce, graphie exacte
    comprise (si l'annonce ecrit "BtoB", ecrire "BtoB" et non "B2B")
  - changer le vocabulaire de l'intitule a seniorite equivalente
    (Account Executive / Ingenieur commercial / Ingenieur d'affaires /
     Responsable de comptes designent le meme metier)
  - piocher dans outils.reservoir tout outil cite par l'annonce

INTERDIT
  - inventer une experience, un chiffre ou un outil absent du reservoir
  - modifier la seniorite d'un poste : ENGIE reste Sales Development Representative
  - gonfler une duree : Comet fait 15 mois, jamais 2 ans
  - ajouter un element de outils.jamais (domaine metier technique)
  - empiler plus de deux methodologies de vente

STRATEGIE
  1. Le titre du CV reprend l'intitule exact de l'annonce quand il est
     compatible avec la seniorite du candidat.
  2. L'accroche est entierement reecrite autour de l'angle de candidature.
     Elle nomme les logos clients pertinents pour CE secteur.
  3. Chaque experience est relue sous l'angle de l'annonce : le contexte de
     l'employeur et les puces changent de formulation, jamais de contenu factuel.
  4. Les competences et outils sont reordonnes et renommes avec le vocabulaire
     de l'annonce. Les outils demandes explicitement remontent en premier.
  5. Les lignes de performance chiffree restent intactes : ce sont les chiffres
     reels du candidat.

LETTRE
Format francais, 4 paragraphes, 250 mots maximum. Pas de formule ampoulee,
pas de "c'est avec un vif interet". Premier paragraphe : pourquoi cette
entreprise et ce poste precisement. Deuxieme : la preuve la plus forte du
parcours, chiffree. Troisieme : ce qui manque, traite de front et retourne.
Quatrieme : disponibilite et demande d'entretien, une phrase.

SORTIE
Reponds UNIQUEMENT en JSON valide, sans preambule ni balises Markdown.

{{
  "titre": "Intitule en tete de CV",
  "accroche": "4 a 6 lignes.",
  "experiences": [
    {{"id": "reality_academy", "intitule": "...", "contexte": "...",
      "puces": ["...", "..."]}},
    {{"id": "nomination", "intitule": "...", "contexte": "...", "puces": []}},
    {{"id": "comet", "intitule": "...", "contexte": "...", "puces": []}},
    {{"id": "engie", "intitule": "Sales Development Representative",
      "contexte": "...", "puces": []}}
  ],
  "competences": [["Label", "contenu"], ["Label", "contenu"]],
  "outils": [["CRM", "..."], ["ERP", "..."]],
  "lettre": "Texte complet de la lettre.",
  "mots_cles_couverts": [],
  "questions_pour_le_candidat": []
}}

Dans questions_pour_le_candidat, liste toute competence ou tout outil cite par
l'annonce dont le niveau du candidat n'est pas determinable depuis le reservoir.
Ne l'ecris PAS dans le CV : une question sera posee au candidat, et sa reponse
enrichira le reservoir."""

# Les lignes de performance sont figees : jamais regenerees par le modele.
PERFS = {
    'reality_academy': "Objectif annuel 415 000 €, réalisé 500 000 €, 120 % d'atteinte.",
    'nomination':      "Objectif annuel 250 000 €, réalisé 280 000 €, 112 % d'atteinte.",
    'comet':           "Objectif annuel 312 000 € de marge brute, réalisé 325 000 €, 104 % d'atteinte.",
    'engie':           '',
}
EMPLOYEURS = {
    'reality_academy': 'Reality Academy, Paris, Île-de-France | 01/2025 - Aujourd’hui',
    'nomination':      'Nomination, Paris, Île-de-France | 01/2023 - 12/2024',
    'comet':           'Comet, Paris, Île-de-France | 10/2021 - 12/2022',
    'engie':           'ENGIE Solutions, Île-de-France | 09/2019 - 08/2021',
}
ORDRE = ['reality_academy', 'nomination', 'comet', 'engie']


def slug(s, n=45):
    s = unicodedata.normalize('NFD', s)
    s = ''.join(c for c in s if unicodedata.category(c) != 'Mn')
    s = re.sub(r'[^A-Za-z0-9]+', '_', s).strip('_')
    return s[:n]


def extraire_json(txt):
    t = txt.strip()
    if t.startswith('```'):
        t = t.split('```')[1]
        if t.startswith('json'):
            t = t[4:]
    d, f = t.find('{'), t.rfind('}')
    if d == -1:
        raise ValueError('pas de JSON')
    return json.loads(t[d:f + 1])


def en_pdf(docx):
    """Conversion via LibreOffice. Silencieuse si absent : le .docx suffit."""
    try:
        subprocess.run(['soffice', '--headless', '--convert-to', 'pdf',
                        '--outdir', str(Path(docx).parent), str(docx)],
                       capture_output=True, timeout=120, check=True)
        pdf = Path(docx).with_suffix('.pdf')
        return str(pdf) if pdf.exists() else ''
    except Exception:
        return ''


def generer(client, system_blocks, offre, reservoir):
    scoring = json.loads(offre['scoring_json'] or '{}')
    prompt = f"""OFFRE

Entreprise : {offre['entreprise']}
Secteur : {offre['secteur']}
Intitule : {offre['titre']}
Lieu : {offre['lieu']}
URL : {offre['url']}

Description :
{(offre['description'] or '')[:12000]}

---
ANALYSE DEJA PRODUITE PAR LE SCORER

Match atteignable : {scoring.get('match_atteignable')}
Angle de candidature : {scoring.get('angle_de_candidature', '')}
Mots-cles a integrer : {', '.join(scoring.get('mots_cles_a_integrer', []))}
Ecarts comblables : {', '.join(scoring.get('ecarts_comblables', []))}
Drapeaux : {', '.join(scoring.get('drapeaux', []))}"""

    r = client.messages.create(model=MODELE, max_tokens=4000,
                               system=system_blocks,
                               messages=[{'role': 'user', 'content': prompt}])
    return extraire_json(r.content[0].text)


def assembler(res, offre, dossier):
    exps = {e['id']: e for e in res.get('experiences', [])}
    d = {
        'nom': 'Mehdi Koriche',
        'titre': res['titre'],
        'contact': [
            'Villiers-sur-Marne, 94350',
            'Téléphone : 06 35 50 02 95',
            'Email : koriche.mehdi@gmail.com',
            'LinkedIn : linkedin.com/in/mehdi-koriche-14673015a',
            'Permis B - Véhicule personnel',
        ],
        'accroche': res['accroche'],
        'experiences': [],
        'competences': [tuple(c) for c in res.get('competences', [])],
        'outils': [tuple(o) for o in res.get('outils', [])],
        'formation': [
            {'diplome': 'Cursus Master Management Commercial - Bac+5, école de commerce',
             'ecole': 'ISEFAC, Paris | 2019 - 2021',
             'contenu': "Négociation grands comptes, stratégie commerciale, "
                        "pilotage d'équipe, marketing stratégique et digital, "
                        "développement international."},
            {'diplome': 'Bachelor Management Commercial',
             'ecole': 'ISEFAC, Paris | 2018 - 2019'},
            {'diplome': 'BTS Négociation et Relation Client',
             'ecole': 'Lycée Jean Lurçat | 2016 - 2018'},
        ],
        'langues': 'Français : langue maternelle · Anglais : professionnel',
    }
    for eid in ORDRE:
        e = exps.get(eid, {})
        d['experiences'].append({
            'intitule': e.get('intitule', ''),
            'employeur': EMPLOYEURS[eid],
            'contexte': e.get('contexte', ''),
            'puces': e.get('puces', []),
            'perf': PERFS[eid],
        })

    base = f"Mehdi_Koriche_CV_{slug(offre['entreprise'],22)}_{slug(offre['titre'],28)}"
    docx = cv_builder.construire(d, str(dossier / f'{base}.docx'))
    pdf = en_pdf(docx)

    lettre = dossier / f'Lettre_{slug(offre["entreprise"],22)}.txt'
    lettre.write_text(res.get('lettre', ''), encoding='utf-8')
    (dossier / 'analyse.json').write_text(
        json.dumps(res, ensure_ascii=False, indent=2), encoding='utf-8')

    return docx, pdf, str(lettre)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--limite', type=int, default=0)
    args = ap.parse_args()

    cle = os.environ.get('ANTHROPIC_API_KEY')
    if not cle:
        print('ANTHROPIC_API_KEY absente.')
        return 1

    cfg = yaml.safe_load(open(CONFIG, encoding='utf-8'))
    reservoir = open(RESERVOIR, encoding='utf-8').read()
    system_blocks = [{'type': 'text',
                      'text': SYSTEM.format(reservoir=reservoir),
                      'cache_control': {'type': 'ephemeral'}}]

    client = Anthropic(api_key=cle)
    cx = sqlite3.connect(cfg['base_de_donnees'])
    cx.row_factory = sqlite3.Row

    q = ("SELECT * FROM offres WHERE statut = 'a_generer' "
         "ORDER BY match_atteignable DESC")
    if args.limite:
        q += f' LIMIT {args.limite}'
    offres = cx.execute(q).fetchall()

    if not offres:
        print('Aucun dossier a generer.')
        return 0
    SORTIE.mkdir(exist_ok=True)
    print(f'{len(offres)} dossier(s) a generer\n')

    ok = 0
    for i, o in enumerate(offres, 1):
        try:
            res = generer(client, system_blocks, o, reservoir)
            dossier = SORTIE / slug(f"{o['entreprise']}_{o['titre']}", 60)
            dossier.mkdir(parents=True, exist_ok=True)
            docx, pdf, lettre = assembler(res, o, dossier)

            cx.execute("""UPDATE offres SET statut='a_envoyer',
                          scoring_json = json_patch(scoring_json, ?)
                          WHERE cle=?""",
                       (json.dumps({'dossier': str(dossier), 'cv_docx': docx,
                                    'cv_pdf': pdf, 'lettre': lettre,
                                    'questions': res.get('questions_pour_le_candidat', [])},
                                   ensure_ascii=False), o['cle']))
            cx.commit()
            ok += 1
            qs = res.get('questions_pour_le_candidat') or []
            print(f'  [{i:2d}] OK {o["match_atteignable"]:3d}  '
                  f'{o["entreprise"][:24]:26s} {o["titre"][:38]}')
            print(f'         {res["titre"][:70]}')
            for q_ in qs:
                print(f'         ? {q_}')
        except Exception as e:
            print(f'  [{i:2d}] ERREUR {o["entreprise"]} : {type(e).__name__} {e}')

    print(f'\n{ok}/{len(offres)} dossiers generes  -> statut a_envoyer')
    print(f'Cout estime : ~{ok*0.05:.2f} EUR')
    return 0


if __name__ == '__main__':
    sys.exit(main())
