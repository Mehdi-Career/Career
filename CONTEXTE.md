# CONTEXTE — à lire avant toute chose

Ce fichier résume une journée entière de travail avec Claude sur l'interface
web. Il existe pour t'éviter de refaire les erreurs qui ont déjà été faites.
Lis-le en entier avant de toucher au code.

---

## 1. Ce que fait ce projet

Système de candidature automatisée pour **Mehdi Koriche**, commercial B2B
grands comptes, 7 ans d'expérience, qui cherche un poste en Île-de-France
chez de grands groupes.

Le pipeline, toutes les heures :

```
crawl.py      interroge les sites carrières de 355 grands groupes IDF
score.py      note chaque offre de 0 à 100 sur son profil (Haiku)
dossier.py    génère un CV adapté + une lettre (Sonnet)
mail.py       lui envoie un mail avec tout prêt ; lit ses réponses
sheet.py      miroir dans Google Sheets
run.py        orchestrateur appelé par le cron GitHub Actions
```

Il postule lui-même sur les portails. Le système ne soumet rien.

**Coût visé : 12 à 15 €/mois** d'API Anthropic. Hébergement gratuit
(GitHub Actions + repo public).

---

## 2. État actuel

| Brique | État |
|---|---|
| Liste de 355 entreprises | ✅ `entreprises-cibles.csv` |
| Domaines des 355 | ✅ `domaines.py` |
| Détection d'ATS | ⚠️ ~195/355 détectées, 145 restantes |
| Crawler + 14 connecteurs | ✅ `crawl.py`, `connecteurs.py` |
| Scoring | ✅ testé sur une offre réelle (SUEZ, 82/100) |
| Génération CV/lettre | ⚠️ **jamais validée visuellement** |
| Mails + suivi | ✅ écrit, jamais tourné en vrai |
| Workflows GitHub | ✅ 6 workflows |

**Le pipeline a tourné une fois** et envoyé 12 offres avec leurs CV.
Personne ne les a regardés. Il est actuellement **désactivé**.

---

## 3. LE PROBLÈME À RÉSOUDRE EN PRIORITÉ

`audit_sites.py` doit visiter chaque site carrières, récupérer le code
source, et identifier la plateforme de recrutement. **Il échoue** :

```
[  1/355] PageGroup France      site injoignable
[  2/355] Hays France           -
[  4/355] ManpowerGroup France  site injoignable
```

Hypothèse non vérifiée : les gros sites institutionnels renvoient 403 aux
requêtes qui ne ressemblent pas à un vrai navigateur. Des en-têtes Chrome
complets ont été ajoutés mais **jamais testés en conditions réelles**.

**Première commande à lancer :**

```bash
python audit_sites.py --diag
```

Elle teste 3 méthodes d'accès sur 5 entreprises et dit laquelle passe.
Corrige jusqu'à ce que l'audit fonctionne, puis lance-le sur les 355.

Si `requests` ne suffit pas, les pistes sont : `curl_cffi` (imite
l'empreinte TLS de Chrome), `cloudscraper`, ou Playwright.

---

## 4. LES PIÈGES DÉJÀ RENCONTRÉS — ne pas les refaire

Quatre faux positifs massifs se sont produits, **toujours pour la même
raison** : valider sur l'absence d'erreur au lieu de valider sur la
présence du résultat attendu.

### 4.1 Les API qui répondent poliment à n'importe quoi

| API | Piège |
|---|---|
| SmartRecruiters | renvoie `{"totalFound":0,"content":[]}` en **200** pour une société inexistante → 355/355 détectées à tort |
| SuccessFactors | sa page d'erreur est servie en **200** et contient les mots `jobreq`, `careersection` → 24/24 détectées à tort |
| Workable | un compte peut exister avec **zéro offre** (cas Randstad) |
| Recruitee | un sous-domaine inexistant **redirige** vers recruitee.com |

**Règle absolue : une détection n'est valide que si la réponse contient
au moins 2 offres réelles avec des identifiants distincts.**
Voir `a_de_vraies_offres()` dans `detect_ats_plus.py`.

Un garde-fou existe : si un ATS dépasse 50 % des résultats, le script
s'arrête en erreur. **Ne le retire pas.**

### 4.2 Les DNS génériques

Ces domaines résolvent **n'importe quel nom**, donc une résolution DNS
n'y prouve rien :

```
icims.com          teamtailor.com          recruitee.com
*.wdN.myworkdayjobs.com
```

Vérifié : `zzqxwvbogus123.wd3.myworkdayjobs.com` résout.
En revanche `talent-soft.com`, `taleo.net`, `avature.net`, `csod.com`
et `sapsf.eu` discriminent correctement.

Voir `DNS_GENERIQUE` dans `detect_ats_plus.py`.

### 4.3 Les tenants génériques

Une extraction naïve donne `www`, `app`, `tt`, `careers`, ou le nom de
la plateforme elle-même comme identifiant. Inutilisable.
Voir `TENANTS_INTERDITS` et `tenant_valide()`.

### 4.4 DuckDuckGo bloque les IP de datacenter

La toute première version cherchait la page carrières via DuckDuckGo.
Échec à 100 % sur les runners GitHub. **Ne repasse pas par un moteur
de recherche.**

---

## 5. LES DÉCOUVERTES UTILES — garde-les

### 5.1 SuccessFactors : l'API universelle

Tout site carrières SuccessFactors (plateforme RMK) expose, **sur son
propre domaine** :

```
POST https://{site-carrieres}/services/recruiting/v1/jobs
{"locale":"fr_FR","pageNumber":0,"sortBy":"","keywords":"","location":"",
 "facetFilters":{},"brand":"","skills":[],"categoryId":0,
 "alertId":"","rcmCandidateId":""}
```

Trouvé dans le code source de `jobs.engie.com`. Plus besoin de deviner
ni le tenant ni le datacenter.

Le vrai identifiant est dans le bloc `j2w.init()` de la page :
```javascript
"ssoCompanyId" : 'engieinforP3',
"ssoUrl"       : 'https://career55.sapsf.eu',
```

**Le domaine est `sapsf.eu`, PAS `successfactors.eu`.** C'est ce qui
expliquait zéro détection pendant des heures.

### 5.2 Talentsoft : motif ultra régulier

```
{nom}-recrute.talent-soft.com
```

Vérifié par DNS : `enedis-recrute`, `cnp-recrute`, `matmut-recrute`,
`casa-recrute` (Crédit Agricole SA), `gsf-recrute`, `radiofrance-recrute`,
`businessfrance-recrute`, `edf-recrute`, `orange-career`,
`airfrance-recrute`, `groupama-recrute`, `maif-career`, `macif-career`,
`spie`.

Autres suffixes vus : `-career`, `-cand`, `-rh`.

### 5.3 Workday : le tenant n'est pas devinable

Il n'existe aucun annuaire public. Le datacenter varie (wd1, wd3, wd5…).
Ce qui marche : tester les combinaisons pod × nom de site sur
`/wday/cxs/{tenant}/{site}/jobs` en POST.

Noms de site confirmés : `SanofiCareers`, `Eiffage_Careers`,
`pernod-ricard`, `NVIDIAExternalCareerSite`.

### 5.4 SNCF : cas d'école du portail maison

`emploi.sncf.com` est un **Next.js**. Vitrine **Talentry**
(`sncf-preprod.talentry.com`), ATS réel **Altays/ClicNJob**
(`altays-progiciels.com/clicnjob/EspaceCand.php?NoSociete=389`).

Tout le contenu des offres est dans `<script id="__NEXT_DATA__">`.
D'où l'extracteur générique `sonde_nextdata()` et le connecteur
`nextdata()` — ils marchent sur n'importe quel site carrières Next.js
sans rien savoir de la plateforme.

---

## 6. Les fichiers

| Fichier | Rôle |
|---|---|
| `entreprises-cibles.csv` | **Remplace le filtre "grand groupe"**. Une entreprise y est, ou elle n'existe pas pour le système |
| `domaines.py` | Domaine web des 355 |
| `reservoir-mehdi-koriche.yaml` | **Le profil complet.** Mémoire du système, rechargée à chaque appel API |
| `reponses-canoniques.yaml` | Les 30 questions des portails |
| `config.yaml` | Filtres, 37 intitulés de poste, seuils |
| `detect_ats.py` | Détection simple (API à slug devinable) |
| `detect_ats_plus.py` | Détection élargie, 7 sondes |
| `audit_sites.py` | **Celui qui est cassé** |
| `importer_urls.py` | Ajout manuel par URL collée |
| `nettoyer_csv.py` | Remise à zéro d'un ATS défaillant |
| `crawl.py` + `connecteurs.py` | Interrogation des 14 ATS |
| `score.py` | Notation |
| `dossier.py` + `cv_builder.py` | CV adapté + lettre |
| `mail.py` | Notifications, réponses, refus |
| `sheet.py`, `run.py` | Sync et orchestration |

---

## 7. Règles de génération du CV — ne pas les assouplir

**Autorisé :** reformuler, réordonner, requalifier ce qui a été
réellement fait. Reprendre littéralement le vocabulaire de l'annonce,
graphie exacte comprise (si l'annonce écrit "BtoB", écrire "BtoB").
Changer le vocabulaire de l'intitulé à séniorité équivalente.

**Interdit :** inventer une expérience, un chiffre ou un outil.
Modifier la séniorité d'un poste (ENGIE reste SDR). Gonfler une durée
(Comet fait 15 mois, jamais 2 ans). Ajouter un élément de
`outils.jamais` (domaine métier technique : CVC, électricité, réseaux
d'eau, normes sectorielles).

Le CV doit rester **ATS-safe** : une colonne, sans photo, sans icône,
sans tableau, **sans en-tête ni pied de page Word** (beaucoup d'ATS les
ignorent), puces Word natives, dates en MM/AAAA, Calibri noir aligné à
gauche, métadonnées propres. Le `.docx` est le format à déposer sur les
portails, mieux parsé que le PDF.

---

## 8. Ce qu'il reste à faire, dans l'ordre

1. **Réparer `audit_sites.py`** (`--diag` d'abord)
2. Lancer l'audit complet, lire `audit/rapport.md`, coder les
   connecteurs des plateformes manquantes
3. **Valider visuellement un CV généré** — jamais fait, et c'est ce qui
   décide si tout le reste sert à quelque chose
4. Réactiver le workflow `pipeline.yml`
5. Ajuster le seuil de scoring (55 actuellement, probablement trop bas)

---

## 9. Deux informations personnelles à corriger

- Le réservoir contient encore le **téléphone personnel** de Mehdi.
  Il n'a pas créé de numéro dédié. Le mail dédié, lui, est bon :
  `koriche.mehdi.career@gmail.com`
- Son LinkedIn affiche **Comet 2021-2023** alors que le CV dit
  **10/2021 – 12/2022**. Les recruteurs croisent les deux sources.
  À aligner côté LinkedIn.

---

## 10. Une leçon de méthode

La journée a été perdue à corriger quatre fois le même type de bug,
parce que Claude codait sans pouvoir tester : son conteneur n'accède
qu'à GitHub, PyPI et npm, jamais aux sites carrières.

Toi, tu peux exécuter. **Lance le code avant de le livrer.** Un run de
diagnostic vaut mieux que trois hypothèses.
