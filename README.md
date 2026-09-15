# Système de candidature — installation

Tout est écrit. Il reste le setup, environ 2 h une seule fois.

---

## Ce que fait le système

Toutes les heures : interroge les sites carrières de 355 grands groupes IDF,
filtre, score chaque offre sur ton profil, génère un CV adapté et une lettre,
et t'envoie un mail avec tout prêt. Tu déposes toi-même sur le portail.

Tes réponses au mail pilotent le suivi. Les refus sont détectés automatiquement.

**Coût : 12 à 15 €/mois** (API Anthropic). Hébergement gratuit.

---

## Étape 1 — Adresse mail dédiée

Crée un Gmail dédié aux candidatures. Tu es en poste, ne mélange pas.

Ensuite il faut un **mot de passe d'application** (pas ton mot de passe Gmail) :

1. Active la validation en deux étapes sur ce compte Google
2. Va sur `myaccount.google.com/apppasswords`
3. Crée un mot de passe, note les 16 caractères

## Étape 2 — Clé API Anthropic

Sur `console.anthropic.com` : crée un compte, génère une clé API, et **configure
tout de suite une limite de dépense mensuelle à 30 €**. Ça ne pourra jamais déraper.

## Étape 3 — Le repo GitHub

Crée un repo **public**. C'est important : sur un compte gratuit, les workflows
planifiés ne se déclenchent pas dans un repo privé.

Le repo ne contient que du code. Tes données (offres, candidatures) vivent dans
le cache GitHub, et tes secrets sont chiffrés même sur un repo public.

Pousse tous les fichiers de ce dossier à la racine du repo.

Puis dans **Settings → Secrets and variables → Actions**, ajoute :

| Secret | Valeur |
|---|---|
| `ANTHROPIC_API_KEY` | ta clé API |
| `MAIL_ADRESSE` | ton mail dédié |
| `MAIL_MDP_APPLICATION` | les 16 caractères de l'étape 1 |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | optionnel, voir étape 5 |
| `SHEET_ID` | optionnel, voir étape 5 |

## Étape 4 — Détecter les ATS

À lancer une fois, sur ton ordinateur ou depuis GitHub :

```bash
pip install -r requirements.txt
python detect_ats.py
```

Une vingtaine de minutes pour les 355 entreprises. Ça remplit `url_carrieres`,
`ats` et `ats_tenant` dans le CSV. **Commit le CSV mis à jour** après.

Les entreprises marquées `inconnu` sont à compléter à la main, ou à laisser :
elles seront simplement ignorées.

## Étape 5 — Google Sheet (optionnel)

Sans ça, le système fonctionne, tu as juste moins de visibilité.

1. Crée un Google Sheet, note son ID (dans l'URL entre `/d/` et `/edit`)
2. Sur `console.cloud.google.com` : nouveau projet, active l'API Google Sheets,
   crée un compte de service, génère une clé JSON
3. Partage ton Sheet avec l'adresse email du compte de service
4. Colle le contenu du JSON dans le secret `GOOGLE_SERVICE_ACCOUNT_JSON`

## Étape 6 — Premier lancement

Onglet **Actions** du repo → *Pipeline candidatures* → **Run workflow**.

Regarde les logs. Le crawl prend 20-30 min la première fois.

Ensuite ça tourne tout seul à la minute 17 de chaque heure.

---

## Ton usage quotidien

Tu reçois un mail par offre retenue, avec le score, la justification, l'angle de
candidature, le CV adapté et la lettre en pièces jointes, et le lien vers l'annonce.

Tu postules, puis **tu réponds au mail avec un seul mot sur la première ligne** :

| Mot | Effet |
|---|---|
| `ok` `oui` `yes` `done` `fait` | Postulé, date enregistrée |
| `non` `no` | Rejetée, sort du pipeline |
| `plus tard` `later` `attente` | Remonte dans 3 jours |
| `entretien` `interview` `rdv` | Passe en entretien |
| `refus` `refusé` | Refus confirmé |

Majuscules et accents sans importance. Un mot seul, rien d'autre sur la ligne.

Les mails de refus des entreprises sont détectés et classés automatiquement.
En cas de doute, le système te demande plutôt que de se tromper.

**Le script ne supprime, n'archive et ne marque rien comme lu.** La connexion
IMAP est ouverte en `readonly` : le protocole lui-même interdit toute écriture.

---

## Réglages

Tout est dans `config.yaml`.

**Trop d'offres ?** Monte `seuil` de 55 à 65 puis 70. Démarre à 55 une semaine
pour voir la réalité, puis ajuste.

**Un intitulé manque ?** Ajoute-le dans `filtres.intitules`.

**Une entreprise t'agace ?** Passe sa colonne `actif` à `non` dans le CSV.

---

## Les fichiers

| Fichier | Rôle |
|---|---|
| `entreprises-cibles.csv` | Les 355 groupes. **Remplace le filtre "grand groupe"** |
| `reservoir-mehdi-koriche.yaml` | Ton profil complet. La mémoire du système |
| `config.yaml` | Filtres, intitulés, seuils |
| `detect_ats.py` | Détection des ATS, à lancer une fois |
| `crawl.py` + `connecteurs.py` | Interrogation des 11 ATS |
| `score.py` | Notation de chaque offre |
| `dossier.py` + `cv_builder.py` | CV adapté et lettre |
| `mail.py` | Notifications, lecture des réponses, refus |
| `sheet.py` | Miroir Google Sheet |
| `run.py` | Orchestrateur appelé par le cron |

---

## Diagnostic

**Aucune offre trouvée** → `detect_ats.py` n'a pas tourné, ou le CSV n'a pas été
commité. Vérifie que la colonne `ats` est remplie.

**Le workflow ne se déclenche pas** → le repo est privé. Passe-le public, ou
prends GitHub Pro. GitHub désactive aussi les crons après 60 jours sans commit.

**Erreurs sur Taleo, Avature, SuccessFactors** → normal, ce sont des connecteurs
HTML fragiles. Si Workday, Greenhouse, Lever, SmartRecruiters ou Ashby échouent,
là c'est un vrai problème : le script te le signale par un `!!`.

**Tester sans rien envoyer** :
```bash
python crawl.py --test Keolis      # une entreprise, sans écriture en base
python score.py --dry-run --limite 5
python dossier.py --limite 1
```

---

## Reste à faire

- Le fichier de réponses canoniques des portails (prétentions, préavis,
  disponibilité) — à écrire ensemble
- Créer les comptes sur les 50 groupes prioritaires, 4 min chacun
- Aligner LinkedIn sur les dates de Comet : 10/2021 – 12/2022
