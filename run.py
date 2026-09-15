#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run.py — orchestrateur. C'est lui que le cron appelle.

  python run.py            # cycle complet
  python run.py --etape score

Ordre : crawl -> score -> dossier -> mail envoyer -> mail lire -> mail refus -> sheet
Une etape qui echoue n'interrompt pas les suivantes : les offres restent
a leur statut et seront reprises au cycle d'apres.
"""
import argparse, subprocess, sys, time
from datetime import datetime

ETAPES = [
    ('crawl',   [sys.executable, 'crawl.py']),
    ('score',   [sys.executable, 'score.py']),
    ('dossier', [sys.executable, 'dossier.py']),
    ('envoyer', [sys.executable, 'mail.py', 'envoyer']),
    ('lire',    [sys.executable, 'mail.py', 'lire']),
    ('refus',   [sys.executable, 'mail.py', 'refus']),
    ('sheet',   [sys.executable, 'sheet.py']),
]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--etape', choices=[e[0] for e in ETAPES])
    a = ap.parse_args()
    etapes = [e for e in ETAPES if not a.etape or e[0] == a.etape]

    print(f"=== Cycle du {datetime.now():%d/%m/%Y %H:%M} ===\n")
    echecs = []
    for nom, cmd in etapes:
        print(f"--- {nom} ---")
        t = time.time()
        r = subprocess.run(cmd)
        if r.returncode:
            echecs.append(nom)
            print(f"  ECHEC (code {r.returncode}), on continue")
        print(f"  {time.time()-t:.0f}s\n")

    if echecs:
        print(f"Etapes en echec : {', '.join(echecs)}")
    return 0
if __name__ == '__main__':
    sys.exit(main())
