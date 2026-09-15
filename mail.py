#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mail.py

Trois roles :
  envoyer   notifie les dossiers prets, CV et lettre en pieces jointes
  lire      lit tes reponses (ok / non / ...) et met la base a jour
  refus     detecte les reponses des entreprises

  python mail.py envoyer
  python mail.py lire
  python mail.py refus
  python mail.py tout

LECTURE SEULE sur la boite : la connexion IMAP est ouverte avec readonly=True,
donc le protocole lui-meme interdit toute ecriture. Aucune suppression,
aucun archivage, aucun marquage lu.

Variables d'environnement :
  MAIL_ADRESSE, MAIL_MDP_APPLICATION, ANTHROPIC_API_KEY
"""

import argparse
import email
import email.utils
import imaplib
import json
import os
import re
import smtplib
import sqlite3
import sys
import unicodedata
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

import yaml
from anthropic import Anthropic

CONFIG = 'config.yaml'
IMAP_HOTE, IMAP_PORT = 'imap.gmail.com', 993
SMTP_HOTE, SMTP_PORT = 'smtp.gmail.com', 465
MODELE = 'claude-haiku-4-5-20251001'

ADRESSE = os.environ.get('MAIL_ADRESSE', '')
MDP = os.environ.get('MAIL_MDP_APPLICATION', '')

# Signatures d'expediteurs ATS
ATS_EXPEDITEURS = re.compile(
    r'myworkdayjobs\.com|greenhouse\.io|lever\.co|ashbyhq\.com|taleo\.net|'
    r'successfactors\.|smartrecruiters\.com|icims\.com|csod\.com|avature\.net|'
    r'talentsoft\.com|talent-soft\.com|no-?reply|donotreply|ne-pas-repondre',
    re.I)

# Vocabulaire de reponse. Le mot doit etre SEUL sur la premiere ligne.
MOTS = {
    'postule':   ['ok', 'oui', 'yes', 'done', 'fait', 'postule'],
    'rejetee':   ['non', 'no'],
    'plus_tard': ['plus tard', 'later', 'attente'],
    'entretien': ['entretien', 'interview', 'rdv'],
    'refus':     ['refus', 'refuse'],
}


def norm(s):
    s = unicodedata.normalize('NFD', s or '')
    s = ''.join(c for c in s if unicodedata.category(c) != 'Mn')
    return re.sub(r'\s+', ' ', s.lower()).strip()


def base(cfg):
    cx = sqlite3.connect(cfg['base_de_donnees'])
    cx.row_factory = sqlite3.Row
    cx.executescript("""
      CREATE TABLE IF NOT EXISTS suivi (
        cle TEXT PRIMARY KEY, date_envoi_mail TEXT, date_candidature TEXT,
        date_reponse TEXT, statut_suivi TEXT DEFAULT 'notifiee', notes TEXT);
      CREATE TABLE IF NOT EXISTS mails_vus (uid TEXT PRIMARY KEY, traite_le TEXT);
    """)
    cx.commit()
    return cx


# ==================================================================
# ENVOI
# ==================================================================

def corps_html(o, sc):
    note = o['match_atteignable']
    couleur = '#1a7f37' if note >= 80 else '#9a6700'
    drapeaux = sc.get('drapeaux') or []
    questions = sc.get('questions') or []

    bloc_d = ''
    if drapeaux:
        bloc_d = ('<p style="background:#fff8c5;padding:10px;border-radius:6px">'
                  '<b>À vérifier :</b> ' + ', '.join(drapeaux) + '</p>')
    bloc_q = ''
    if questions:
        li = ''.join(f'<li>{q}</li>' for q in questions)
        bloc_q = ('<p style="background:#ddf4ff;padding:10px;border-radius:6px">'
                  f'<b>Questions avant dépôt :</b><ul>{li}</ul></p>')

    return f"""<div style="font-family:-apple-system,Segoe UI,sans-serif;
      max-width:640px;line-height:1.5;color:#1f2328">
      <p style="font-size:13px;color:#656d76;margin:0">{o['entreprise']} · {o['secteur']}</p>
      <h2 style="margin:4px 0 8px">{o['titre']}</h2>
      <p style="font-size:26px;font-weight:700;color:{couleur};margin:0">{note}<span
         style="font-size:15px;color:#656d76;font-weight:400">/100</span></p>
      <p style="color:#656d76;margin:2px 0 14px;font-size:13px">
         CV actuel : {o['match_brut']}/100 · adaptation {sc.get('effort_adaptation','')}</p>
      <p>{sc.get('justification','')}</p>
      <p><b>Angle :</b> {sc.get('angle_de_candidature','')}</p>
      {bloc_d}{bloc_q}
      <p style="font-size:13px;color:#656d76">
         {o['lieu'] or 'lieu non précisé'} · ATS {o['ats']}</p>
      <p><a href="{o['url']}" style="background:#0969da;color:#fff;padding:11px 20px;
         border-radius:6px;text-decoration:none;font-weight:600">Voir l'annonce</a></p>
      <p style="font-size:13px;color:#656d76">CV et lettre en pièces jointes.</p>
      <hr style="border:0;border-top:1px solid #d0d7de;margin:18px 0">
      <p style="font-size:13px"><b>Réponds à ce mail avec un seul mot :</b><br>
         <code>ok</code> j'ai postulé &nbsp;·&nbsp;
         <code>non</code> pas intéressé &nbsp;·&nbsp;
         <code>plus tard</code> &nbsp;·&nbsp;
         <code>entretien</code></p>
      <p style="font-size:11px;color:#8c959f">réf {o['cle'][:40]}</p>
    </div>"""


def envoyer(cx, limite=0):
    q = ("SELECT * FROM offres WHERE statut='a_envoyer' "
         "ORDER BY match_atteignable DESC")
    if limite:
        q += f' LIMIT {limite}'
    offres = cx.execute(q).fetchall()
    if not offres:
        print('Rien a envoyer.')
        return

    with smtplib.SMTP_SSL(SMTP_HOTE, SMTP_PORT) as s:
        s.login(ADRESSE, MDP)
        for o in offres:
            sc = json.loads(o['scoring_json'] or '{}')
            m = EmailMessage()
            marque = '[PUSH] ' if o['match_atteignable'] >= 80 else ''
            m['Subject'] = (f"{marque}{o['match_atteignable']}/100 · "
                            f"{o['entreprise']} · {o['titre'][:60]}")
            m['From'], m['To'] = ADRESSE, ADRESSE
            m['X-Offre-Cle'] = o['cle']
            m.set_content(f"{o['titre']} chez {o['entreprise']}\n"
                          f"{o['match_atteignable']}/100\n{o['url']}\n\n"
                          f"Reponds : ok / non / plus tard / entretien\n"
                          f"ref {o['cle']}")
            m.add_alternative(corps_html(o, sc), subtype='html')

            for chemin in (sc.get('cv_docx'), sc.get('cv_pdf'), sc.get('lettre')):
                if chemin and Path(chemin).exists():
                    d = Path(chemin).read_bytes()
                    ext = Path(chemin).suffix.lstrip('.')
                    maj, sub = ('application', 'octet-stream')
                    if ext == 'pdf':
                        maj, sub = 'application', 'pdf'
                    elif ext == 'txt':
                        maj, sub = 'text', 'plain'
                    m.add_attachment(d, maintype=maj, subtype=sub,
                                     filename=Path(chemin).name)

            s.send_message(m)
            maintenant = datetime.now(timezone.utc).isoformat()
            cx.execute("UPDATE offres SET statut='notifiee' WHERE cle=?", (o['cle'],))
            cx.execute("""INSERT OR REPLACE INTO suivi
                          (cle, date_envoi_mail, statut_suivi)
                          VALUES (?,?, 'notifiee')""", (o['cle'], maintenant))
            cx.commit()
            print(f"  envoye  {o['match_atteignable']:3d}  "
                  f"{o['entreprise'][:24]:26s} {o['titre'][:40]}")
    print(f'\n{len(offres)} mail(s) envoye(s)')


# ==================================================================
# LECTURE - strictement readonly
# ==================================================================

def ouvrir_imap():
    im = imaplib.IMAP4_SSL(IMAP_HOTE, IMAP_PORT)
    im.login(ADRESSE, MDP)
    im.select('INBOX', readonly=True)      # <- verrou protocole
    return im


def texte_brut(msg):
    if msg.is_multipart():
        for p in msg.walk():
            if p.get_content_type() == 'text/plain':
                try:
                    return p.get_payload(decode=True).decode(
                        p.get_content_charset() or 'utf-8', 'replace')
                except Exception:
                    continue
        return ''
    try:
        return msg.get_payload(decode=True).decode(
            msg.get_content_charset() or 'utf-8', 'replace')
    except Exception:
        return ''


def premiere_ligne_utile(corps):
    """Premiere ligne non vide qui n'est pas une citation."""
    for ligne in corps.splitlines():
        l = ligne.strip()
        if not l or l.startswith('>') or l.startswith('Le ') or 'a écrit' in l:
            continue
        return l
    return ''


def interpreter(corps):
    l = norm(premiere_ligne_utile(corps))
    if not l or len(l) > 20:
        return None
    for action, mots in MOTS.items():
        if l in [norm(m) for m in mots]:
            return action
    return None


def lire_reponses(cx):
    im = ouvrir_imap()
    _, data = im.search(None, 'ALL')
    uids = data[0].split()[-300:]
    traites = 0

    for uid in uids:
        u = uid.decode()
        if cx.execute('SELECT 1 FROM mails_vus WHERE uid=?', (u,)).fetchone():
            continue
        _, d = im.fetch(uid, '(RFC822)')
        msg = email.message_from_bytes(d[0][1])

        exp = email.utils.parseaddr(msg.get('From', ''))[1].lower()
        if ADRESSE.lower() not in exp:
            continue

        sujet = msg.get('Subject', '')
        m = re.search(r'ref ([a-f0-9|].{10,})', texte_brut(msg))
        cle = m.group(1).strip() if m else None
        if not cle:
            m2 = re.search(r'·\s*(.+?)\s*·', sujet)
            if not m2:
                continue
            r = cx.execute("SELECT cle FROM offres WHERE entreprise=? "
                           "ORDER BY vue_le DESC LIMIT 1", (m2.group(1),)).fetchone()
            cle = r['cle'] if r else None
        if not cle:
            continue

        action = interpreter(texte_brut(msg))
        if not action:
            continue

        maintenant = datetime.now(timezone.utc).isoformat()
        MAJ = {
            'postule':   ('postulee', 'date_candidature'),
            'rejetee':   ('rejetee_manuel', None),
            'plus_tard': ('reportee', None),
            'entretien': ('entretien', 'date_reponse'),
            'refus':     ('refusee', 'date_reponse'),
        }
        statut, champ = MAJ[action]
        cx.execute('UPDATE offres SET statut=? WHERE cle=?', (statut, cle))
        if champ:
            cx.execute(f'UPDATE suivi SET statut_suivi=?, {champ}=? WHERE cle=?',
                       (statut, maintenant, cle))
        else:
            cx.execute('UPDATE suivi SET statut_suivi=? WHERE cle=?', (statut, cle))
        cx.execute('INSERT OR REPLACE INTO mails_vus VALUES (?,?)', (u, maintenant))
        cx.commit()
        traites += 1
        print(f'  {action:10s} -> {cle[:56]}')

    im.logout()
    print(f'{traites} reponse(s) traitee(s)')


# ==================================================================
# DETECTION DES REFUS
# ==================================================================

CLASSIF = """Tu classes un email recu par un candidat apres une candidature.

Reponds UNIQUEMENT par un mot parmi :
  refus              le candidat n'est pas retenu
  entretien          on lui propose un entretien ou un echange
  accuse_reception   simple confirmation de reception, aucune decision
  autre              sans rapport avec une candidature
  doute              tu n'es pas certain

En cas d'hesitation, reponds doute. Il vaut mieux demander au candidat que de
classer un entretien comme un refus."""


def detecter_refus(cx, client):
    im = ouvrir_imap()
    _, data = im.search(None, 'ALL')
    uids = data[0].split()[-300:]
    ouvertes = cx.execute("""SELECT o.cle, o.entreprise, o.titre FROM offres o
                             JOIN suivi s ON s.cle=o.cle
                             WHERE o.statut IN ('postulee','notifiee','relancee')"""
                          ).fetchall()
    if not ouvertes:
        im.logout()
        return

    n_refus = n_entretien = n_doute = 0

    for uid in uids:
        u = 'refus_' + uid.decode()
        if cx.execute('SELECT 1 FROM mails_vus WHERE uid=?', (u,)).fetchone():
            continue
        _, d = im.fetch(uid, '(RFC822)')
        msg = email.message_from_bytes(d[0][1])
        exp = msg.get('From', '')
        if ADRESSE.lower() in exp.lower():
            continue

        corps = texte_brut(msg)
        sujet = msg.get('Subject', '')
        blob = norm(f'{exp} {sujet} {corps}')

        # Couche 2 : rapprochement avec une candidature ouverte
        candidate = None
        for o in ouvertes:
            if norm(o['entreprise']).split()[0] in blob:
                candidate = o
                break
        if not candidate:
            continue

        # Couche 1 : expediteur ATS -> confiance elevee
        est_ats = bool(ATS_EXPEDITEURS.search(exp))

        # Couche 3 : classification
        r = client.messages.create(
            model=MODELE, max_tokens=10, system=CLASSIF,
            messages=[{'role': 'user',
                       'content': f'De : {exp}\nObjet : {sujet}\n\n{corps[:3000]}'}])
        verdict = r.content[0].text.strip().lower()
        if not est_ats and verdict in ('refus', 'entretien'):
            verdict = 'doute'          # sans signature ATS, on ne tranche pas seul

        maintenant = datetime.now(timezone.utc).isoformat()
        if verdict == 'refus':
            cx.execute("UPDATE offres SET statut='refusee' WHERE cle=?", (candidate['cle'],))
            cx.execute("UPDATE suivi SET statut_suivi='refusee', date_reponse=? WHERE cle=?",
                       (maintenant, candidate['cle']))
            n_refus += 1
            print(f"  refus      {candidate['entreprise']}")
        elif verdict == 'entretien':
            cx.execute("UPDATE offres SET statut='entretien' WHERE cle=?", (candidate['cle'],))
            cx.execute("UPDATE suivi SET statut_suivi='entretien', date_reponse=? WHERE cle=?",
                       (maintenant, candidate['cle']))
            n_entretien += 1
            print(f"  ENTRETIEN  {candidate['entreprise']}")
        elif verdict == 'doute':
            demander(candidate, exp, sujet, corps)
            n_doute += 1
            print(f"  doute -> question envoyee : {candidate['entreprise']}")

        cx.execute('INSERT OR REPLACE INTO mails_vus VALUES (?,?)', (u, maintenant))
        cx.commit()

    im.logout()
    print(f'{n_refus} refus · {n_entretien} entretien(s) · {n_doute} a confirmer')


def demander(offre, exp, sujet, corps):
    m = EmailMessage()
    m['Subject'] = f"À confirmer · {offre['entreprise']} · {offre['titre'][:50]}"
    m['From'], m['To'] = ADRESSE, ADRESSE
    m.set_content(
        f"Un mail est arrive et je n'ai pas su le classer.\n\n"
        f"De : {exp}\nObjet : {sujet}\n\n{corps[:1200]}\n\n"
        f"---\nReponds : refus / entretien / autre\nref {offre['cle']}")
    with smtplib.SMTP_SSL(SMTP_HOTE, SMTP_PORT) as s:
        s.login(ADRESSE, MDP)
        s.send_message(m)


# ==================================================================

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('action', choices=['envoyer', 'lire', 'refus', 'tout'])
    ap.add_argument('--limite', type=int, default=0)
    args = ap.parse_args()

    if not ADRESSE or not MDP:
        print('MAIL_ADRESSE et MAIL_MDP_APPLICATION requis.')
        return 1

    cfg = yaml.safe_load(open(CONFIG, encoding='utf-8'))
    cx = base(cfg)

    if args.action in ('envoyer', 'tout'):
        envoyer(cx, args.limite)
    if args.action in ('lire', 'tout'):
        lire_reponses(cx)
    if args.action in ('refus', 'tout'):
        client = Anthropic(api_key=os.environ['ANTHROPIC_API_KEY'])
        detecter_refus(cx, client)
    return 0


if __name__ == '__main__':
    sys.exit(main())
