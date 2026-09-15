#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sheet.py — miroir lisible de la base dans Google Sheets.

Ecriture seule, tu n'as rien a saisir : les statuts viennent de tes reponses
mail et de la detection automatique des refus.

Variables : GOOGLE_SERVICE_ACCOUNT_JSON (contenu du fichier), SHEET_ID
"""
import json, os, sqlite3, sys
import yaml

EN_TETES = ['Date', 'Entreprise', 'Secteur', 'Poste', 'Score', 'Statut',
            'Date candidature', 'Date reponse', 'Relance', 'ATS', 'Lien']

def main():
    sa = os.environ.get('GOOGLE_SERVICE_ACCOUNT_JSON')
    sid = os.environ.get('SHEET_ID')
    if not sa or not sid:
        print('Sheet non configure, etape ignoree.')
        return 0

    import gspread
    from google.oauth2.service_account import Credentials
    creds = Credentials.from_service_account_info(
        json.loads(sa),
        scopes=['https://www.googleapis.com/auth/spreadsheets'])
    ws = gspread.authorize(creds).open_by_key(sid).sheet1

    cfg = yaml.safe_load(open('config.yaml', encoding='utf-8'))
    cx = sqlite3.connect(cfg['base_de_donnees'])
    cx.row_factory = sqlite3.Row
    lignes = cx.execute("""
        SELECT o.vue_le, o.entreprise, o.secteur, o.titre, o.match_atteignable,
               o.statut, s.date_candidature, s.date_reponse, o.ats, o.url
        FROM offres o LEFT JOIN suivi s ON s.cle = o.cle
        WHERE o.statut NOT IN ('a_scorer','rejetee_score','rejetee_mur')
        ORDER BY o.vue_le DESC""").fetchall()

    from datetime import datetime, timezone, timedelta
    corps = []
    for r in lignes:
        relance = ''
        if r['statut'] == 'postulee' and r['date_candidature']:
            d = datetime.fromisoformat(r['date_candidature'])
            j = (datetime.now(timezone.utc) - d).days
            relance = 'A RELANCER' if j >= 7 else f'J+{j}'
        corps.append([
            (r['vue_le'] or '')[:10], r['entreprise'], r['secteur'], r['titre'],
            r['match_atteignable'], r['statut'],
            (r['date_candidature'] or '')[:10], (r['date_reponse'] or '')[:10],
            relance, r['ats'], r['url']])

    ws.clear()
    ws.update([EN_TETES] + corps, 'A1')
    ws.format('A1:K1', {'textFormat': {'bold': True}})
    print(f'{len(corps)} ligne(s) synchronisee(s)')
    return 0
if __name__ == '__main__':
    sys.exit(main())
