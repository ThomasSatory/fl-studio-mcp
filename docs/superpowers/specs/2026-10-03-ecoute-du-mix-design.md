# Écoute du mix — rendu hors ligne et analyse (spec)

## But

Le modèle compose et mixe à l'aveugle : `fl_get_mixer_levels` ne donne que des
crêtes instantanées pendant la lecture. On veut qu'il puisse **écouter le
morceau entier** : le rendre en WAV, mesurer niveau, dynamique, équilibre
spectral et stéréo, section par section, et en option piste par piste
(stems), puis lui signaler les problèmes en termes de producteur (« le 808 et
le kick se battent dans le sub », « refrain 9 LU plus fort que le couplet »).

## Faits vérifiés en direct (spike du 2026-10-03, FL 2025.2 sur macOS)

- `OsxFL -R -Ewav <fichier.flp>` rend `<fichier>.wav` à côté du fichier
  (48 kHz, stéréo, float32), puis le processus s'arrête. Il faut ~22 s pour
  un morceau de 145 s, démarrage compris. La forme Windows `/R /Ewav` ne fait
  qu'ouvrir le projet sur macOS.
- Fonctionne pendant que l'instance principale de FL est ouverte : c'est un
  second processus qui rend, et la session de l'utilisateur n'est pas touchée.
  Il rend le fichier **sur disque**, il faut donc sauvegarder avant.
- Deux rendus simultanés : un seul aboutit, les autres sortent sans fichier.
  Les rendus doivent être sérialisés.
- Stems : sur une copie, effacer le bit `0x08` (Enabled) du u32 à l'offset 4
  de l'événement 236 (drapeaux d'insert) de chaque piste à couper. Le bit
  Solo `0x1000` est ignoré par le rendu. Il faut garder la piste cible **et sa
  chaîne aval** (bus), sinon le stem est silencieux. Les stems de bus
  additionnés redonnent le mix à 1e-7 près.
- Ne jamais couper le dernier insert (index 126 sur 127, la piste « Current »
  de FL 2025) : FL n'écrit alors aucun fichier, et le code de sortie vaut 0.
- L'événement 235 (un par insert, dans le même ordre que les 236) est un
  tableau d'octets indexé par destination, où un octet non nul signifie une
  route. Il correspond exactement à `mixer.getRouting` en direct, y compris
  les routes de sidechain à niveau 0.

## Conception

### Unités

1. **`utils/flp.py`** (ajouts) : lecture du routage (235) et des drapeaux
   (236), coupure d'inserts sur une copie, calcul de l'ensemble à garder
   pour un stem. Ce sont des fonctions pures, testées avec `tests/flp_builder.py`.
   - Ensemble gardé pour la cible T = {0} ∪ {T} ∪ amont(T) ∪ aval(T), où
     amont = tout insert dont le signal atteint T, et aval = tout insert
     atteint depuis T. Garder l'amont fait d'un bus le stem « tout ce qui
     passe par ce bus ». Les routes à niveau 0 (sidechain) ne laissent pas
     fuir de son : leur seule sortie audible passe par des inserts coupés.
2. **`utils/render.py`** : trouve l'exécutable FL, lance le rendu d'une copie
   dans un dossier temporaire, sous un verrou inter-processus, avec un délai
   maximal. Range le WAV dans un dossier de rendus et garde les 30 plus
   récents. Lève `ToolError` si le processus dépasse le délai (il est tué),
   ou s'il n'y a pas de fichier.
3. **`utils/mix_analysis.py`** (numpy) : lecture WAV (PCM 16/24/32, float
   32/64, extensible, chunks JUNK), sonie intégrée BS.1770 (LUFS), sonie
   court terme max, crête échantillon et true peak (suréchantillonnage x4),
   RMS, facteur de crête, échantillons au-dessus de 0 dBFS, 5 bandes
   (sub < 60, low 60–250, low_mid 250–2k, high_mid 2k–6k, air > 6k), largeur
   stéréo et corrélation, silence en début et en fin, analyse par sections,
   avertissements, comparaison des stems.
4. **`tools/render.py`** : l'outil `fl_render_mix`, qui orchestre le tout.

### Outil `fl_render_mix`

Paramètres : `stems` (None | liste de numéros de pistes du mixer | `"buses"` |
`"all"`), `sections` (bool, défaut True), `save` (défaut True),
`project_path`.

Déroulé : sauvegarde (Ctrl+S) et localisation du `.flp` (logique existante,
factorisée dans `project_files.current_project_file`) ; rendu du mix
complet ; analyse globale et par sections. Les sections viennent des
marqueurs de l'arrangement courant, ou à défaut de blocs de 8 mesures. Les
mesures sont converties en secondes avec le tempo du projet ; une
automation de tempo décale les temps, ce qu'une note signale. Viennent
ensuite les stems s'ils sont demandés (8 au plus par appel, environ 22 s
chacun), puis la comparaison des stems.

Pour `"all"`, on prend les inserts qui reçoivent au moins un canal
(`channels.getAll` → `target_fx_track` ≠ 0), plus les bus. Pour `"buses"`,
les inserts ≠ 0 qui ont au moins une entrée parmi les inserts utilisés. Les
canaux routés directement sur le Master sont présents dans chaque stem : un
avertissement le dit.

Réponse : `project`, `render` (chemin du WAV, durée de rendu), `analysis`
(global, sections, silence, avertissements), `stems` (piste, nom, chemin,
LUFS, crête, bandes, part d'énergie dans le mix, `silent`), `stem_warnings`,
`notes`.

### Stems sans fuite (ajouté après la revue finale, vérifié en direct)

Garder tout l'amont et tout l'aval laisse fuir du son. Une source en amont
qui va aussi au Master laisse passer son signal sec dans le stem d'une
reverb ; un sidechain de niveau 0 fait entrer la basse dans le stem du kick.
Sur la copie du stem :

- l'aval ne suit que les routes audio (niveau > 0, lu en direct par
  `mixer.getRouting`), avec repli sur le fichier et une note si le
  contrôleur ne répond pas ;
- les routes des inserts gardés en amont vers toute destination hors de
  amont(T) ∪ {T} sont mises à zéro dans leur événement 235, sans changer sa
  longueur. Il est vérifié en direct que le moteur de rendu de FL respecte
  une route mise à zéro ;
- ce qui fuit encore (un insert en aval qui porte ses propres canaux) est
  signalé dans `also_contains` et `stem_warnings` ;
- `compare_stems` ne compare pas un stem de bus avec ses propres sources.

Un bus est un insert (ni 0 ni le dernier) qui reçoit au moins deux inserts
utilisés. Un simple envoi ou sidechain n'en fait donc pas un.

L'outil est async : le travail bloquant tourne dans des threads, et la
progression est envoyée après le mix et après chaque stem, pour les clients
dont le délai est de 60 s.

### Erreurs

Pas d'exécutable FL → message qui indique `FL_STUDIO_MCP_FL_BINARY`. Délai
dépassé → processus tué, message « FL attend peut-être un dialogue (plugin
manquant, démo) ». Pas de WAV → erreur explicite, jamais traitée comme du
silence. Projet illisible ou non réécrivable octet pour octet → refus des
stems seulement, le mix complet reste rendu. Insert hors limites → `ToolError`.

### Plateformes

macOS vérifié. Windows : `FL64.exe /R /Ewav <fichier>` (forme documentée par
Image-Line), non vérifié, signalé comme tel dans le README.

### Tests

Unitaires pour chaque unité. Le rendu est testé avec un faux exécutable
(script Python qui écrit un WAV à côté du `.flp`) ; l'outil, avec la
connexion factice existante. Vérification en direct sur MCP-TEST à la fin.

### Hors périmètre

Rendu d'une plage de mesures (on rend tout et on analyse la plage), formats
autres que WAV, stems de canaux (seulement des inserts du mixer), correction
automatique du mix.
