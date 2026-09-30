# Ergonomie pour le modèle — design

Date : 2026-09-30
Dépôt : fork privé `ThomasSatory/fl-studio-mcp`, branche `ergonomie-modele` depuis `main` (096f4c0, après fusion de `fiabilisation`)

## 1. Contexte et objectif

Le serveur expose 76 outils. Les erreurs d'un assistant qui compose viennent surtout de l'interface, pas d'API FL manquantes :

- les instructions du serveur sont périmées (« Cannot create new patterns » alors que `fl_new_pattern` existe ; piano roll, undo, fenêtres absents) ;
- aucun outil ne porte d'annotation MCP, le client ne distingue pas lecture et écriture destructive ;
- des outils redondants consomment du contexte (`fl_get_channel_count` et `fl_get_all_channels`, cinq outils de plugin qui lisent chacun un champ) ;
- le modèle doit convertir lui-même accords et noms de notes en numéros MIDI ;
- le piano roll avale les erreurs : dans `ComposeWithLLM.pyscript`, un `add_notes` en échec laisse le résumé à `status: "success"` avec `notes_added: 0`, et l'outil répond sans erreur.

Objectif : un modèle qui lit moins, se trompe moins sur les notes, et apprend tout échec.

## 2. Hors périmètre

- Nouvelles capacités FL (playlist, routage mixer, sauvegarde, vumètres, paramètres de plugin par nom).
- Commande groupée en un aller-retour, table de dispatch du contrôleur.
- Progressions en degrés, gammes, quantification.
- Propriétés de note `fcut`, `fres`, `pitchofs`.
- Fusion des setters par domaine (`fl_mixer_set(...)`) : seuls les doublons stricts sont supprimés.
- `install.ps1`.

## 3. Instructions du serveur et README

`FastMCP(instructions=...)` dans `server.py` est réécrit :

- les dix domaines réels : transport et tempo, mixer, canaux et step sequencer, plugins, piano roll, patterns, undo, fenêtres, projet, connexion ;
- premier appel conseillé : `fl_get_project_snapshot` ;
- piano roll : une fenêtre de piano roll doit être ouverte ; ComposeWithLLM doit avoir été lancé une fois à la main par session FL (Outils > Scripting) ; temps et durées en quarts de temps ; `fl_open_piano_roll(channel)` pour choisir le canal cible ;
- **convention d'octave : C4 = 60** ; l'interface de FL Studio affiche 60 comme « C5 » ;
- limites exactes : pas de chargement de plugin ; `fl_trigger_note` est temps réel et ne persiste pas ;
- la phrase fausse sur la création de patterns est retirée.

La docstring du module `server.py` et le README reçoivent les mêmes corrections, plus la liste des outils supprimés et leurs remplaçants (section 5).

## 4. Annotations

Chaque `@mcp.tool` reçoit `annotations=` (`ToolAnnotations` ou dict, FastMCP 4.0.10). Quatre jeux sont définis une fois dans `src/fl_studio_mcp/tools/__init__.py` :

| Constante | Valeur | Outils |
|---|---|---|
| `READ_ONLY` | `readOnlyHint=True` | `fl_get_*`, `fl_list_*`, `fl_connection_status`, `fl_get_project_snapshot`, `fl_get_pr_context`, `fl_get_piano_roll_info`, `fl_get_undo_status` |
| `IDEMPOTENT` | `readOnlyHint=False, destructiveHint=False, idempotentHint=True` | réglages absolus : volume, pan, nom, couleur, séparation stéréo, tempo, position, mode de boucle, vitesse, mute/solo/arm avec valeur explicite, `fl_set_grid_bit`, `fl_select_channel`, `fl_select_pattern`, `fl_rename_pattern`, `fl_set_plugin_param_value`, fenêtres |
| `DESTRUCTIVE` | `readOnlyHint=False, destructiveHint=True` | `fl_clear_piano_roll`, `fl_delete_notes`, `fl_send_notes` (mode `replace` possible), `fl_set_step_sequence`, `fl_undo`, `fl_redo`, `fl_clear_request_queue` |
| `ACTION` | `readOnlyHint=False, destructiveHint=False, idempotentHint=False` | `fl_play`, `fl_stop`, `fl_record`, `fl_trigger_note`, `fl_send_chord`, `fl_trigger_script`, `fl_next_preset`, `fl_prev_preset`, `fl_new_pattern`, `fl_clone_pattern`, `fl_save_undo_point` |

Un outil mute/solo/arm qui bascule l'état (valeur absente) va dans `ACTION` ; le classement exact de chaque outil est fait à l'implémentation en lisant sa signature, la règle ci-dessus tranche. Un test (section 8) exige une annotation sur chaque outil enregistré.

## 5. Suppression des doublons (76 → 64 outils)

Les commandes du contrôleur restent toutes : le snapshot et le piano roll les utilisent (`patterns.getCurrent`, `channels.getSelected`). Seuls les outils MCP disparaissent.

| Outil supprimé | Remplaçant |
|---|---|
| `fl_get_channel_count` | `fl_get_all_channels` |
| `fl_get_selected_channel` | `fl_get_all_channels` (champ `is_selected`) |
| `fl_select_one_channel` | `fl_select_channel(index, select=True, exclusive=False)` ; `exclusive=True` envoie `channels.selectOne` |
| `fl_get_grid_bit` | `fl_get_step_sequence` |
| `fl_get_mixer_track_count` | `fl_get_all_mixer_tracks` |
| `fl_is_plugin_valid` | `fl_get_plugin_params` |
| `fl_get_plugin_name` | `fl_get_plugin_params` |
| `fl_get_plugin_param_count` | `fl_get_plugin_params` |
| `fl_get_preset_count` | `fl_get_plugin_params` |
| `fl_get_plugin_color` | `fl_get_plugin_params` |
| `fl_get_current_pattern` | `fl_list_patterns` (champ `current`) |
| `fl_connect` | `fl_connection_status(reconnect=False)` ; `reconnect=True` appelle `reset_connection()` avant la tentative |

Remplaçants enrichis :

- **`channels.getAll`** (contrôleur) ajoute `volume`, `pan`, `is_solo` à chaque canal. Le snapshot, qui réutilise ce handler, en profite.
- **`fl_get_all_mixer_tracks`** renvoie `{"track_count": mixer.trackCount(), "tracks": [...]}` au lieu d'une liste ; `mixer.getAllTracks` ajoute `track_count`. `track_count` compte toutes les pistes, `tracks` reste filtré par `include_empty`.
- **`plugins.getParams`** (contrôleur) renvoie `{valid, name, param_count, preset_count, color, params}`. Plugin invalide : `valid: false`, `params: []`, autres champs absents, **pas d'erreur**. L'outil renvoie ce dict au lieu d'une liste.

`fl_set_grid_bit` et `fl_get_song_length` restent.

Le contrôleur modifié doit être recopié dans FL Studio (`scripts/setup.sh` le fait) ; le message « contrôleur périmé » existant couvre l'oubli.

## 6. Théorie musicale côté serveur

Nouveau module `src/fl_studio_mcp/utils/music.py`, fonctions pures, sans dépendance, lèvent `ValueError` (les outils la convertissent en `ToolError`).

### 6.1 `note_to_midi(name: str) -> int`

- Grammaire : lettre `A`–`G` (insensible à la casse), altérations `#` ou `b` (une ou deux, `##`/`bb` acceptés), octave entière de -1 à 9. Espaces autour ignorés.
- C4 = 60. `Cb4` = 59, `E#4` = 65, `B#3` = 60.
- Résultat hors 0–127 (`G#9`, `Cb-1`) ou nom invalide → `ValueError` citant l'entrée.

### 6.2 `chord_to_midi(symbol: str, octave: int = 4, inversion: int = 0) -> list[int]`

- `symbol` = fondamentale sans octave + qualité + basse optionnelle `/X`.
- Qualités et intervalles (demi-tons depuis la fondamentale) :

| Qualité | Intervalles |
|---|---|
| (vide), `maj` | 0 4 7 |
| `m`, `min` | 0 3 7 |
| `dim` | 0 3 6 |
| `aug`, `+` | 0 4 8 |
| `sus2` | 0 2 7 |
| `sus4`, `sus` | 0 5 7 |
| `5` | 0 7 |
| `6` | 0 4 7 9 |
| `m6` | 0 3 7 9 |
| `7` | 0 4 7 10 |
| `maj7` | 0 4 7 11 |
| `m7` | 0 3 7 10 |
| `mM7` | 0 3 7 11 |
| `m7b5` | 0 3 6 10 |
| `dim7` | 0 3 6 9 |
| `9` | 0 4 7 10 14 |
| `maj9` | 0 4 7 11 14 |
| `m9` | 0 3 7 10 14 |
| `add9` | 0 4 7 14 |
| `11` | 0 4 7 10 14 17 |
| `13` | 0 4 7 10 14 21 |

- Qualité inconnue → `ValueError` listant les qualités acceptées.
- La fondamentale est placée dans `octave` (`A`, octave 3 → 57).
- `inversion=n` (0 ≤ n < nombre de notes) : les n notes les plus graves montent d'une octave, avant l'ajout de la basse. Hors bornes → `ValueError`.
- Basse `/X` : note `X` placée sous la note la plus grave de l'accord (octave la plus haute qui reste strictement en dessous), ajoutée en tête.
- Liste triée croissante ; une note hors 0–127 → `ValueError`.
- Exemples de référence : `("Am7", 3)` → `[57, 60, 64, 67]` ; `("Cmaj9/E", 4)` → `[52, 60, 64, 67, 71, 74]` ; `("C", 4, inversion=1)` → `[64, 67, 72]`.

### 6.3 `midi_to_note_name(midi: int) -> str`

Déplacé depuis `piano_roll.py` (`_midi_to_note_name`), comportement inchangé (dièses, C4 = 60).

## 7. Piano roll

### 7.1 Entrée et validation (serveur)

Une fonction `_validate_notes(notes, *, require_duration=True) -> list[dict]` construit des notes normalisées **sans modifier la liste reçue** et lève `ToolError` au premier défaut, en citant l'index (« note 3 : velocity 1.4 hors de 0–1 »). Rien n'est écrit dans la file si une note est invalide.

Champs d'une note :

| Champ | Règle | Défaut |
|---|---|---|
| `midi` / `note` | exactement un des deux ; `midi` entier 0–127 ; `note` passé à `note_to_midi` | — |
| `time` | nombre ≥ 0 | 0 |
| `duration` | nombre > 0 (obligatoire sauf suppression) | — |
| `velocity` | 0–1 | 0,8 |
| `pan` | 0–1 | 0,5 |
| `color` | entier 0–15 | 0 |
| `slide` | booléen | faux |
| `porta` | booléen | faux |

Champ inconnu → `ToolError` (évite une faute de frappe silencieuse comme `veloctiy`). Les notes envoyées au script portent toujours `midi`, jamais `note`.

- **`fl_send_notes(notes, mode="add", auto_trigger=True)`** : `mode` hors `add`/`replace` → `ToolError`.
- **`fl_send_chord(chord, octave=4, inversion=0, time=0, duration=1.0, velocity=0.8, pan=0.5, color=0, auto_trigger=True)`** : `chord` est un symbole (`"Am7"`) résolu par `chord_to_midi`, ou une liste de numéros MIDI et/ou de noms (`[57, "C4", "E4"]`) — `octave` et `inversion` ne s'appliquent qu'au symbole. Le paramètre `midi_notes` est renommé `chord`. Les notes passent par `_validate_notes` et partent en `add_notes` (l'action `add_chord` du script reste pour la compatibilité mais n'est plus émise).
- **`fl_delete_notes(notes, auto_trigger=True)`** : accepte `midi` ou `note`, et `time` ; `duration` non requise.
- Réponse : les notes résolues nommées, par exemple « Am7 → A3 C4 E4 G4 à 0, durée 4 ».

### 7.2 Script `ComposeWithLLM.pyscript`

- `SCRIPT_VERSION = 2`.
- `add_notes_to_piano_roll` et `add_chord_to_piano_roll` appliquent `pan`, `color`, `slide`, `porta` quand présents.
- `process_mcp_request` : chaque sous-action en échec est enregistrée `{"index": i, "action": ..., "message": ...}` ; une action inconnue est un échec. Après la boucle : si au moins un échec, `status: "error"`, `message` résumant le premier échec, `failures` la liste ; les compteurs `notes_added` / `notes_deleted` reflètent ce qui a été appliqué. FL n'a pas de transaction : ce qui a réussi reste.
- Le résumé contient toujours `script_version` et `note_count` (`flp.score.noteCount` après traitement).
- La file est vidée dans tous les cas, comme aujourd'hui.

### 7.3 Vérification (serveur)

Après `_run_pr_script()` avec `ran: true` et une réponse :

1. `status == "error"` → `ToolError` : « FL Studio a appliqué N note(s) ajoutée(s), M supprimée(s), puis l'action i (add_notes) a échoué : … ».
2. Réponse sans `script_version` (script v1) :
   - si la requête contenait `pan`, `color`, `slide` ou `porta` hors défaut → `ToolError` demandant de réinstaller ComposeWithLLM (`scripts/setup.sh`) ; les notes ont été écrites sans ces propriétés, le message le dit ;
   - sinon, succès avec l'avertissement « ComposeWithLLM est périmé, réinstallez-le ».
3. `fl_send_notes` / `fl_send_chord` : `notes_added` différent du nombre de notes envoyées → `ToolError` citant les deux nombres.
4. Succès : « 4 note(s) écrite(s) (A3 C4 E4 G4 …). Le piano roll contient maintenant 36 notes. »

`auto_trigger=False` : mise en file sans vérification ; la réponse dit que rien n'est vérifié tant que `fl_trigger_script` n'a pas été appelé. `fl_trigger_script` applique les étapes 1 et 2 (pas 3 : il ne sait pas ce qui était en file).

## 8. Tests

Tous sans FL Studio : `uv run pytest -q`, `uv run ruff check src fl_controller tests`.

| Fichier | Vérifie |
|---|---|
| `tests/test_music.py` | table de cas `note_to_midi` (naturelles, `#`, `b`, `##`, `Cb4`, `E#4`, `B#3`, octave -1, bornes, invalides) ; `chord_to_midi` pour chaque qualité, renversements, basses, les trois exemples de 6.2, erreurs |
| `tests/test_tool_registry.py` | les 12 outils de la section 5 sont absents ; chaque outil enregistré a des annotations ; `fl_get_project_snapshot` est `readOnlyHint`, `fl_clear_piano_roll` `destructiveHint` |
| `tests/test_note_validation.py` | chaque règle du tableau 7.1 ; `midi` et `note` ensemble ou absents ; champ inconnu ; la liste d'entrée n'est pas modifiée ; la file n'est pas écrite en cas d'erreur ; `fl_send_chord` symbole et liste mixte |
| `tests/test_pianoroll_script.py` | faux `flpianoroll` (`score` avec `PPQ`, `noteCount`, `getNote`, `addNote`, `deleteNote` ; `Note`) ajouté à `tests/fakes.py`, script chargé par `importlib` depuis son chemin ; propriétés appliquées ; échec partiel → `status: "error"`, compteurs partiels, `failures` ; action inconnue ; `script_version` et `note_count` présents ; file vidée |
| `tests/test_pr_verification.py` | réponse en erreur → `ToolError` avec comptes ; script v1 + propriétés étendues → `ToolError` ; script v1 sans → avertissement ; `notes_added` décalé → `ToolError` ; `auto_trigger=False` |
| `tests/test_controller.py` (ajouts) | `channels.getAll` avec `volume`, `pan`, `is_solo` ; `mixer.getAllTracks` avec `track_count` ; `plugins.getParams` valide et invalide |

Les tests existants qui appellent un outil supprimé ou renommé sont adaptés vers le remplaçant.

`docs/superpowers/specs/manual-checks.md` gagne :

7. `fl_send_chord("Am7", octave=3)` → FL affiche les notes A4, C5, E5, G5 dans son propre étiquetage (A3–G4 en convention C4 = 60) ; la réponse dit « 4 note(s) écrite(s) ».
8. `fl_send_notes([{"note": "C4", "duration": 1, "slide": true}])` → la note apparaît en slide.
9. Remettre l'ancien `ComposeWithLLM.pyscript`, refaire 8 → erreur demandant la réinstallation.

## 9. Ordre de réalisation

1. `music.py` et ses tests.
2. Validation des notes, `fl_send_notes` / `fl_send_chord` / `fl_delete_notes`.
3. Faux `flpianoroll`, script v2 (propriétés, erreurs, version, `note_count`).
4. Vérification côté serveur.
5. Contrôleur enrichi (`channels.getAll`, `mixer.getAllTracks`, `plugins.getParams`) et suppression des 12 outils.
6. Annotations et test du registre.
7. Instructions, docstring, README, `manual-checks.md`.

Chaque étape : tests d'abord, implémentation, `pytest` et `ruff` verts, commit. Aucun push vers `upstream`.
