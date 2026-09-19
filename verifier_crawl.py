#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
verifier_crawl.py

REPOND A LA QUESTION : "est-ce qu'on recupere vraiment les annonces ?"

Avoir l'URL d'un site carrieres ne prouve rien. Ce script passe CHAQUE
entreprise configuree dans son connecteur et compte les offres reellement
obtenues. Il ne suppose rien, il mesure.

Pour chaque entreprise il rapporte :
  - le nombre d'offres brutes recuperees
  - combien survivent aux filtres (intitule, lieu, contrat, anciennete)
  - trois exemples de titres, pour verifier d'un coup d'oeil que ce sont
    bien des offres et pas des liens de navigation

Et il classe le resultat :
  OK        des offres arrivent, le crawl fonctionne
  VIDE      le connecteur repond mais ne trouve rien : URL a revoir
  ERREUR    le connecteur plante

Le CSV n'est PAS modifie. C'est un diagnostic, pas une correction.

  python -u verifier_crawl.py                 # toutes les configurees
  python -u verifier_crawl.py --priorite 1
  python -u verifier_crawl.py --test Danone
  python -u verifier_crawl.py --limite 30
"""

import csv
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

import yaml

from connecteurs import CONNECTEURS
from crawl import Filtres, charger_entreprises

CONFIG = 'config.yaml'
PARALLELISME = 6
RAPPORT = 'audit/verification-crawl.md'


def verifier(row, filtres, max_offres=200):
    """(statut, nb_brutes, nb_retenues, exemples, erreur)"""
    fn = CONNECTEURS.get(row['ats'])
    if not fn:
        return 'SANS CONNECTEUR', 0, 0, [], f"aucun connecteur pour {row['ats']}"

    t0 = time.time()
    try:
        brutes = fn(row.get('ats_tenant', ''), row.get('url_carrieres', ''),
                    max_offres)
    except Exception as e:
        return 'ERREUR', 0, 0, [], f'{type(e).__name__}: {e}'

    duree = time.time() - t0
    retenues = []
    for o in brutes:
        o.entreprise = row['nom']
        garde, _ = filtres.verdict(o)
        if garde:
            retenues.append(o)

    exemples = [o.titre[:70] for o in (retenues or brutes)[:3]]
    statut = 'OK' if brutes else 'VIDE'
    return statut, len(brutes), len(retenues), exemples, f'{duree:.0f}s'


def main():
    args = sys.argv[1:]
    cfg = yaml.safe_load(open(CONFIG, encoding='utf-8'))
    filtres = Filtres(cfg)

    if '--test' in args:
        nom = ' '.join(args[args.index('--test') + 1:])
        rows = [r for r in charger_entreprises(nom=nom)]
        if not rows:
            print(f'{nom} : introuvable ou non configuree')
            return 1
        row = rows[0]
        print(f"{row['nom']}")
        print(f"  ats    : {row['ats']}")
        print(f"  tenant : {row.get('ats_tenant') or '(aucun)'}")
        print(f"  url    : {row.get('url_carrieres') or '(aucune)'}\n")
        st, nb, ret, ex, info = verifier(row, filtres)
        print(f"  {st} - {nb} offres brutes, {ret} retenues apres filtres  ({info})")
        for t in ex:
            print(f"     . {t}")
        return 0

    priorite = None
    if '--priorite' in args:
        priorite = int(args[args.index('--priorite') + 1])
    entreprises = charger_entreprises(priorite=priorite)
    if '--limite' in args:
        entreprises = entreprises[:int(args[args.index('--limite') + 1])]

    print(f'{len(entreprises)} entreprises configurees a verifier')
    print(f'{PARALLELISME} en parallele. Le CSV ne sera PAS modifie.\n',
          flush=True)

    lignes, stats = [], Counter()
    total_brutes = total_retenues = 0

    with ThreadPoolExecutor(max_workers=PARALLELISME) as pool:
        futurs = {pool.submit(verifier, r, filtres): r for r in entreprises}
        for i, fut in enumerate(as_completed(futurs), 1):
            row = futurs[fut]
            try:
                st, nb, ret, ex, info = fut.result()
            except Exception as e:
                st, nb, ret, ex, info = 'ERREUR', 0, 0, [], str(e)

            stats[st] += 1
            total_brutes += nb
            total_retenues += ret
            lignes.append((row['nom'], row['ats'], st, nb, ret, ex, info))

            marque = {'OK': 'OK  ', 'VIDE': '-- ', 'ERREUR': '!! ',
                      'SANS CONNECTEUR': '?? '}.get(st, '   ')
            print(f'  [{i:3d}/{len(entreprises)}] {marque} '
                  f'{row["nom"][:28]:30s} {row["ats"]:16s} '
                  f'{nb:4d} brutes -> {ret:3d} retenues', flush=True)
            if ex and st == 'OK':
                print(f'            . {ex[0]}', flush=True)

    print(f'\n{"=" * 62}')
    for st, n in stats.most_common():
        print(f'  {n:4d}  {st}')
    print(f'\n  {total_brutes} offres brutes au total')
    print(f'  {total_retenues} retenues apres filtres '
          f'(intitule, IDF, CDI, moins de 30 jours)')
    if stats['OK']:
        print(f'  soit ~{total_retenues / max(stats["OK"], 1):.1f} offres '
              f'pertinentes par entreprise qui repond')
    print('=' * 62)

    ecrire_rapport(lignes, stats, total_brutes, total_retenues)
    return 0


def ecrire_rapport(lignes, stats, brutes, retenues):
    import os
    os.makedirs('audit', exist_ok=True)
    out = ['# Verification du crawl', '',
           'Chaque entreprise configuree passee dans son connecteur.',
           'Mesure reelle, pas estimation.', '',
           '## Bilan', '']
    for st, n in stats.most_common():
        out.append(f'- {st} : {n}')
    out += ['', f'- offres brutes : {brutes}',
            f'- retenues apres filtres : {retenues}', '',
            '## Detail', '',
            '| Entreprise | ATS | Statut | Brutes | Retenues | Exemple |',
            '|---|---|---|---:|---:|---|']
    for nom, ats, st, nb, ret, ex, info in sorted(lignes, key=lambda x: (-x[3], x[0])):
        ex1 = (ex[0] if ex else info)[:60].replace('|', ' ')
        out.append(f'| {nom} | {ats} | {st} | {nb} | {ret} | {ex1} |')
    open(RAPPORT, 'w', encoding='utf-8').write('\n'.join(out) + '\n')
    print(f'\n-> {RAPPORT} ecrit')


if __name__ == '__main__':
    sys.exit(main())
