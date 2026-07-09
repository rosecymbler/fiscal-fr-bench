# Spec — Annotation Gold Standard

> Protocole d'annotation manuelle pour mesurer le **vrai recall** du linking jurisprudence ↔ CGI/LPF de FiscalQA Pro. Cette annotation servira de **ground truth** pour le papier.



---

## 🎯 Objectif

Annoter exhaustivement les références à des articles du **CGI ou du LPF** dans **50 décisions de jurisprudence fiscale** tirées au hasard.

Cette annotation permet de calculer :

- **Recall (vrai)** = articles trouvés par notre pipeline ∩ articles annotés par toi / articles annotés par toi
- **Précision (cross-check)** = articles trouvés par notre pipeline ∩ articles annotés par toi / articles trouvés par notre pipeline

**Note importante** : ce qu'on appelle "annoter" ici, c'est lister exhaustivement TOUS les articles CGI/LPF effectivement cités dans la décision — pas évaluer l'application de l'article au litige.

---

## 📋 Périmètre de l'annotation

### Ce que tu DOIS annoter
Toute référence textuelle à un article appartenant à :
- **Code général des impôts (CGI)** — corps principal
- **CGI annexe I, II, III, IV**
- **Livre des procédures fiscales (LPF)**

### Ce que tu NE DOIS PAS annoter
- Articles d'autres codes (Code civil, CPP, Code des douanes, Code de commerce, etc.)
- Lois numérotées (`loi n° 2008-776 du 4 août 2008`)
- Conventions, décrets, arrêtés, directives
- Références à des décisions antérieures (`l'arrêt CE Sect. 13 mars 1991`)

### Cas limites — comment trancher

| Cas | À annoter ? |
|---|---|
| *"l'article 209 B du CGI"* | ✅ Oui |
| *"l'article 209 B"* (CGI sous-entendu) | ✅ Oui (si contexte clair fiscal) |
| *"ledit article 209 B"* / *"le même article"* | ✅ Oui (résoudre la coréférence) |
| *"l'article 209 B dans sa rédaction antérieure à la loi n° X"* | ✅ Oui (la loi n'est qu'une référence pour préciser la version) |
| *"l'article 1382 du Code civil"* (mention CGI ailleurs) | ❌ Non |
| *"l'article 8 de la Convention européenne"* | ❌ Non |
| *"l'article 33 de la loi n° 94-126"* | ❌ Non (article d'une loi, pas du CGI) |

---

## 🗂️ Format d'annotation

Tu vas recevoir un **CSV de 50 décisions** avec ces colonnes :

| Colonne | Description |
|---|---|
| `n°` | Numéro de la décision dans le sample (1 à 50) |
| `jurisprudence_id` | ID interne de la décision |
| `source` | `arianeweb` (CE) / `inca` (Cass) / `judilibre` |
| `juridiction` | Conseil d'État / Cour de cassation |
| `formation` | Chambre / formation jugeante |
| `date_decision` | Date de la décision |
| `numero_pourvoi` | Numéro de pourvoi |
| `texte_url` | Lien Légifrance pour lecture |
| `texte_extrait` | Premiers 5 000 caractères (motifs + dispositif typiquement) |
| **`articles_cites`** | **À remplir : liste des articles CGI/LPF cités, séparés par `;`** |
| **`commentaire`** | **Optionnel : précisions / ambiguïtés** |

### Format pour `articles_cites`

Format : `<CODE> <num>` séparés par `;` — exemples :

```
CGI 209 B; CGI 238 A
LPF L. 16 B; LPF L. 47; CGI 1729
CGI annexe IV 23 H; CGI 39 quinquies
CGI 197; CGI 156; CGI 158
```

**Règles de format** :
- `<CODE>` : `CGI`, `CGI annexe I`, `CGI annexe II`, `CGI annexe III`, `CGI annexe IV`, `LPF`
- `<num>` : tel qu'il est cité dans la décision (ex: `209 B`, `L. 16 B`, `1011 bis`, `R. 281-1`)
- Séparateur entre articles : `;`
- Si **aucun article CGI/LPF n'est cité** dans la décision (la décision a été tirée par erreur dans l'échantillon fiscal) : laisser la colonne vide et noter `aucun` dans `commentaire`
- Si **doute** : annoter quand même + noter le doute dans `commentaire`

### Exemple complet

```csv
n°,jurisprudence_id,source,juridiction,formation,date_decision,numero_pourvoi,texte_url,texte_extrait,articles_cites,commentaire
1,aw__Ariane_Web_AW_DCE__410428,arianeweb,Conseil d'État,9ème - 10ème chambres réunies,2018-06-12,410428,https://...,"...En application de l'article 209 B du code général des impôts...",CGI 209 B; CGI 238 A,
2,inca_JURITEXT000007408584,inca,Cour de cassation,Chambre commerciale,2000-02-22,99-13.142,https://...,"...visa de l'article L. 16 B du livre des procédures fiscales...",LPF L. 16 B,
3,judi_613727f1cd5801467742ea3e,judilibre,Cour de cassation,Chambre commerciale,2011-11-10,10-19.236,https://...,"...l'article 669 du code général des impôts ayant remplacé l'article 762...",CGI 669; CGI 762,
```

---

## ⏱️ Workflow recommandé

### Étape 1 
Annote les 5 premières décisions en **lecture rapide** + relecture. Calibre ton œil sur ce qui compte.

### Étape 2 
Annote les 45 restantes. Compte ~5 minutes par décision. Pour gagner du temps :
- Lis **uniquement** les motifs et le dispositif (le factuel et la procédure n'ont quasi jamais de citations CGI)
- Utilise **Ctrl+F** dans le `texte_extrait` pour repérer "article", "CGI", "code général des impôts", "L.", "R."
- Si la décision ne mentionne aucun article CGI/LPF dans les 5 000 chars d'extrait, ouvre l'URL Légifrance pour vérifier rapidement

### Étape 3 
Relis les 5 cas où tu avais un commentaire / doute. Confirme ou corrige.

---

## 🚫 Pièges fréquents 

1. **Les visas Judilibre** sont parfois imprécis (ex: visa dit "L16" mais le texte cite "L. 16 B"). Annote ce que tu vois dans le **texte de la décision**, pas dans le visa.
2. **Les num avec préfixe** (L. 47, R. 281-1) doivent être annotés au format complet avec le préfixe.

---

## 📊 Comment on utilisera ton annotation

Une fois que tu as fini :

1. Tu rends le CSV avec colonne `articles_cites` remplie
2. On compare automatiquement avec ce que notre pipeline a linké pour les 50 mêmes décisions :
   - Articles trouvés par toi mais pas par nous = **faux négatifs (FN)** → permet de calculer le recall
   - Articles trouvés par nous mais pas par toi = soit FP, soit articles que tu as oubliés → on revoit ensemble pour valider
3. Le résultat consolidé = précision + recall **vraies**, à présenter dans le papier
4. Le CSV annoté devient une **section data** du repo (avec ton accord)

---

## ❓ FAQ

> **Q. Une décision cite l'article 209 B trois fois dans le texte. Je l'annote une fois ou trois fois ?**

R. Une seule fois. On annote la **liste des articles cités** (set), pas la liste des occurrences.

> **Q. La décision cite "l'article 209 B (devenu 209 B-0)". Je mets quoi ?**

R. Les deux : `CGI 209 B; CGI 209 B-0`. Le commentaire peut préciser "renumérotation".

> **Q. Une décision cite "l'art. 1649 nonies A". Je mets quoi ?**

R. Si c'est dans le CGI : `CGI 1649 nonies A`. Si c'est dans une annexe (à vérifier — il y en a aussi dans annexe I/II/III/IV), précise.

> **Q. La décision dit "le même article" sans préciser lequel. Je remonte plus haut ?**

R. Oui, fait la coréférence. Si "le même article" renvoie à l'article 209 B mentionné 2 paragraphes plus haut, annote `CGI 209 B`.

> **Q. Je tombe sur une décision qui n'a aucun lien fiscal pertinent. Comment l'identifier ?**

R. Ça arrive (~5-10% de l'échantillon stratifié). Laisse `articles_cites` vide et écris `aucun` dans `commentaire`. On l'exclura du calcul de recall.

---

## 🛠️ How the sample is generated

Le CSV de 50 décisions sera produit par le script `scripts/sample_for_gold_standard.py` (à venir), qui fait :
- Tirage stratifié proportionnel : 25 unified + 12 inca + 10 arianeweb + 3 judilibre
- Seed reproductible (seed=2026)
- Filtrage : décisions avec ≥3 000 chars (suffisamment de matière à analyser)
- Exclusion : décisions qui sont déjà dans le sample audit n=100 (pour éviter le double-test)

Quand tu reçois le CSV, ouvre-le dans Numbers / Excel / Google Sheets et remplis la colonne `articles_cites`.

---

*Spec préparée par Rose, 10 mai 2026. Questions / ambiguïtés → me ping directement.*
