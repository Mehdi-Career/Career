#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
nettoyer_csv.py

Remet a zero les detections d'un ATS donne, quand une sonde s'est revelee
defaillante. Les autres lignes ne sont pas touchees.

  python -u nettoyer_csv.py successfactors
  python -u nettoyer_csv.py successfactors workable recruitee
  python -u nettoyer_csv.py --tout        # tout remettre a zero
"""
import csv, sys
from collections import Counter

CSV = 'entreprises-cibles.csv'

def main():
    cibles = [a.lower() for a in sys.argv[1:]]
    if not cibles:
        print('Precise au moins un ATS a nettoyer, ou --tout')
        return 1

    rows = list(csv.DictReader(open(CSV, encoding='utf-8')))
    avant = Counter(r['ats'] for r in rows)
    n = 0
    for r in rows:
        if '--tout' in cibles or (r.get('ats') or '').lower() in cibles:
            r['ats'] = ''
            r['ats_tenant'] = ''
            r['url_carrieres'] = ''
            n += 1

    with open(CSV, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=rows[0].keys())
        w.writeheader()
        w.writerows(rows)

    print(f'{n} ligne(s) remise(s) a zero\n')
    print('Avant :')
    for a, c in avant.most_common():
        print(f'  {c:4d}  {a or "(vide)"}')
    print('\nApres :')
    for a, c in Counter(r['ats'] for r in rows).most_common():
        print(f'  {c:4d}  {a or "(vide)"}')
    exploitables = sum(1 for r in rows if r['ats'] not in ('', 'inconnu'))
    print(f'\n{exploitables}/{len(rows)} entreprises exploitables')
    return 0

if __name__ == '__main__':
    sys.exit(main())
