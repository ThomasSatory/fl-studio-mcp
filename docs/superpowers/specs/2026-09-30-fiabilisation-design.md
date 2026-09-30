# Fiabilisation de fl-studio-mcp — design

Date : 2026-09-30
Dépôt : fork `ThomasSatory/fl-studio-mcp`, branche `fiabilisation`
Socle : `main` du fork = `upstream/main` (2b31051) + fusion telle quelle de la PR upstream #5 (giang17/linux-wine, 1d5b67c)

## 1. Contexte et objectif

fl-studio-mcp pilote FL Studio depuis un client MCP. Deux canaux :

- **MIDI + fichiers JSON** pour transport, mixer, canaux, plugins, patterns, undo, fenêtres : le serveur écrit `mcp_command.json`, envoie une note MIDI 127 sur un port virtuel, le contrôleur `device_FLStudioMCP.py` (Python embarqué de FL Studio) exécute l'API et écrit `mcp_response.json`.
- **Fichiers JSON + raccourci clavier** pour le piano roll : le serveur écrit `mcp_request.json`, active FL Studio, envoie Cmd+Opt+Y, et le script `ComposeWithLLM.pyscript` (contexte Python séparé, module `flpianoroll`) applique les notes et exporte `piano_roll_state.json`.

Usage cible : Claude Desktop ou Claude Code sur un Mac, compte personnel, transport stdio. Rapport à upstream : hybride, corrections de bugs en petites PR compatibles, nouveautés dans le fork.

Objectif : rendre le canal MIDI + fichiers déterministe (identifiant, verrou, port sûr), rendre le piano roll honnête sur macOS (pas de lancement de FL Studio à l'insu de l'utilisateur, pas de chevauchement), ajouter le tempo et une vue d'ensemble du projet, uniformiser les erreurs, tester sans FL Studio, corriger l'installateur.

## 2. Hors périmètre

- Transport SysEx bidirectionnel (issue upstream #7). Non nécessaire sur macOS où les fichiers fonctionnent.
- Regroupement ou renommage des outils existants (compatibilité upstream).
- `install.ps1` (Windows, non testable ici) : intouché, mention dans le README.
- Piano roll : format des requêtes, unités (quarts de temps), propriétés de notes supplémentaires.
- Restauration du focus de l'application précédente après le déclenchement du piano roll.
- Contrôle du tempo par un autre moyen que l'événement REC (aucune autre voie dans l'API).

## 3. Branches et découpage

| Branche | Base | Contenu |
|---|---|---|
| `main` (fork) | upstream/main + PR #5 | socle, jamais modifié à la main |
| `fiabilisation` | `main` | tout le travail du fork (sections 4 à 8) |
| `fix/no-hardware-port-fallback` | `upstream/main` | PR upstream 1 : sélection stricte du port + variable d'environnement |
| `fix/no-launch-fl-on-trigger` | `upstream/main` | PR upstream 2 : vérifier que FL Studio tourne avant de l'activer |
| `fix/installer-absolute-uv` | `upstream/main` | PR upstream 3 : installateur (chemin absolu de uv, `claude mcp add`, détection IAC par ports MIDI, mode non interactif) |

Les PR upstream réimplémentent sur `upstream/main` (sans PR #5) la même logique que la branche `fiabilisation`, en version minimale. Elles sont ouvertes en fin de chantier, après que la branche `fiabilisation` est verte.

## 4. Protocole MIDI + fichiers

### 4.1 Serveur (`src/fl_studio_mcp/utils/midi_connection.py`)

- **Identifiant de requête.** `send_command` génère `uuid4().hex`, écrit `{"id", "action", "params"}`. `_wait_for_response(request_id, timeout)` ne retourne qu'une réponse dont `id == request_id`. Une réponse portant un autre `id` est supprimée et l'attente continue. Une réponse **sans** `id` est ignorée mais mémorisée : au délai dépassé, le message d'erreur dit alors que le script contrôleur installé dans FL Studio est périmé et doit être réinstallé.
- **Verrou.** `threading.Lock` sur `MIDIConnection`, pris pour tout le cycle écriture → suppression de l'ancienne réponse → note MIDI → attente. FastMCP 4 exécute les outils synchrones dans un pool de threads, donc deux appels d'outils peuvent être concurrents.
- **Écriture atomique côté serveur.** Le fichier de commande est écrit dans `mcp_command.json.tmp` puis `os.replace` (5 essais espacés de 20 ms sur `PermissionError`, cas Windows). Le serveur tourne dans un CPython normal, le renommage y est sûr.
- **Réponse illisible.** Un JSON tronqué ou invalide n'est pas une erreur : l'attente continue (comportement PR #5 conservé). Le délai dépassé avec un fichier jamais lisible produit le message existant « never became readable ».
- **Sélection du port.** Fonction pure `select_output_port(available: list[str], requested: str | None) -> str` :
  1. si `requested` (variable `FL_STUDIO_MCP_MIDI_PORT`) : correspondance exacte, sinon sous-chaîne insensible à la casse ; aucune correspondance → `PortSelectionError` listant les ports ;
  2. sinon, pour chaque indice dans l'ordre `("IAC", "loopMIDI", "Midi Through")`, le premier port dont le nom contient l'indice (insensible à la casse) ;
  3. sinon `PortSelectionError` : liste des ports vus, comment activer le bus IAC (macOS) ou loopMIDI (Windows), et comment poser la variable.
  Aucun repli sur un port quelconque, jamais. L'indice `FL` seul disparaît (il correspond à du matériel comme le Novation FLkey). La branche Linux de PR #5 (« Midi Through » obligatoire) est couverte par l'indice 3.
- `connect()` appelle `select_output_port` et convertit `PortSelectionError` en `self._error`.

### 4.2 Contrôleur (`fl_controller/device_FLStudioMCP.py`)

- `execute_pending_command` lit `id` dans la commande et le recopie dans la réponse, y compris dans les réponses d'erreur (JSON invalide, fichier absent : `id` vaut alors `null`).
- Écriture **en place** (`write_text`), jamais par fichier temporaire + renommage : dans FL Studio 2026, `os.replace` fait échouer l'interpréteur embarqué de façon définitive (constat PR upstream #9). Le texte est sérialisé avant l'ouverture du fichier pour qu'une valeur non sérialisable ne tronque jamais le fichier.
- Les erreurs de handler restent `{"success": false, "error": "..."}` (PR #5).

### 4.3 Compatibilité

Un serveur neuf avec un contrôleur ancien : délai dépassé avec message explicite (voir 4.1). Un contrôleur neuf avec un serveur ancien : le serveur ancien ignore la clé `id`, rien ne casse.

## 5. Piano roll sur macOS

Fichier : `src/fl_studio_mcp/utils/fl_trigger.py`, `src/fl_studio_mcp/tools/piano_roll.py`.

- **Processus FL Studio.** `FLStudioTrigger.is_fl_running() -> bool` :
  - macOS : `osascript -e 'tell application "System Events" to (exists process "FL Studio")'` ;
  - Windows : énumération de fenêtres déjà présente (`_focus_fl_studio_windows` trouve une fenêtre) ;
  - Linux : `_find_fl_windows_linux()` non vide.
  `activate_window()` et `_trigger_macos()` retournent `False` sans rien faire si FL Studio ne tourne pas. Plus jamais de `activate` sur une application fermée (AppleScript la lancerait).
- **Message.** Quand FL Studio ne tourne pas, `_run_pr_script` retourne un échec dont le texte dit « FL Studio n'est pas lancé », distinct de « le script n'a pas tourné ». Les outils de notes remontent ce texte via `ToolError` (section 7).
- **Verrou piano roll.** `threading.Lock` au niveau du module `piano_roll.py`, pris par chaque outil de notes pour l'ensemble écriture de la file → focus → raccourci → attente. Deux appels concurrents s'exécutent l'un après l'autre. Ce verrou est distinct de celui du protocole MIDI : le focus du piano roll passe par le contrôleur, donc par le verrou MIDI, à l'intérieur du verrou piano roll. Ordre d'acquisition toujours piano roll puis MIDI, jamais l'inverse, donc pas d'interblocage.
- **Conservé de PR #5** : activation, focus par le contrôleur, attente d'une preuve d'exécution (réponse du script ou nouvelle version du fichier d'état), compte rendu honnête.

## 6. Nouveaux outils

### 6.1 Tempo (`src/fl_studio_mcp/tools/transport.py`, contrôleur)

- `fl_get_tempo() -> dict` : action `transport.getTempo`, contrôleur `mixer.getCurrentTempo()`. Retour `{"tempo": float}`.
- `fl_set_tempo(bpm: float) -> dict` : validation serveur `10 <= bpm <= 522` (limites FL Studio), sinon `ToolError`. Action `transport.setTempo` ; contrôleur :
  `general.processRECEvent(midi.REC_Tempo, int(round(bpm * 1000)), midi.REC_Control | midi.REC_UpdateValue | midi.REC_UpdateControl)` puis relecture `mixer.getCurrentTempo()`. Réponse `{"requested": bpm, "tempo": relu, "applied": |relu - bpm| < 0.01}`. Côté serveur, `applied` faux → `ToolError` « tempo demandé X, FL Studio rapporte Y ».
- Les instructions du serveur (`server.py`) mentionnent déjà « tempo » ; elles deviennent vraies.

### 6.2 Snapshot (`src/fl_studio_mcp/tools/project.py`, nouveau, contrôleur)

- `fl_get_project_snapshot() -> dict` : une action `project.snapshot`, un seul aller-retour. Le contrôleur assemble :
  - `transport` : résultat de `handle_transport_get_status()` ;
  - `tempo` : `mixer.getCurrentTempo()` ;
  - `patterns` : `{"current": n, "count": c, "items": [...]}` à partir des handlers patterns de PR #5 ;
  - `channels` : `handle_channels_get_all()["channels"]` ;
  - `mixer_tracks` : `handle_mixer_get_all_tracks({"include_empty": False})["tracks"]`.
  Chaque bloc est calculé dans son propre `try`, une section en échec vaut `{"error": "..."}` sans faire tomber les autres.
- Le piano roll n'en fait pas partie (autre fichier, autre contexte) ; `fl_get_pr_context` de PR #5 reste l'outil dédié.
- Délai : `send_command(..., timeout=5.0)` pour cette action, le reste garde 2 s.

## 7. Erreurs uniformes

- Nouveau module `src/fl_studio_mcp/utils/errors.py` :
  - `def unwrap(result: dict) -> dict` : si `result.get("success") is False` ou clé `error` présente sans `success` vrai, lève `fastmcp.exceptions.ToolError(result["error"])` ; sinon retourne `result`.
  - `def run(conn, action, params=None, timeout=2.0) -> dict` : `ensure_connected` (convertit `RuntimeError` en `ToolError`), `send_command`, `unwrap`.
- Tous les outils des modules `transport`, `mixer`, `channels`, `plugins`, `patterns`, `history`, `ui`, `piano_roll` et `server.py` remplacent leurs branches `return f"Error: …"`, `return {"error": …}`, `return [{"error": …}]`, `return -1` par `run(...)` ou `raise ToolError(...)`.
- Les valeurs de retour en cas de succès ne changent pas (chaînes descriptives, dicts, listes, entiers).
- `fl_connect` et `fl_connection_status` gardent leur sémantique de diagnostic : `fl_connection_status` ne lève jamais, il décrit ; `fl_connect` lève `ToolError` si la connexion échoue.
- Les tests de PR #5 qui vérifient des chaînes « Error: … » sont adaptés à `pytest.raises(ToolError)`.

## 8. Tests

Répertoire `tests/`, pytest, sans FL Studio. Ajout de `tests/fakes.py` (adapté de la PR upstream #9) :

- `install_fl_fakes(monkeypatch)` : insère dans `sys.modules` des modules `channels`, `midi`, `mixer`, `plugins`, `transport`, `patterns`, `general`, `ui` sous forme de `MagicMock` configurés (valeurs de retour typées : `mixer.getCurrentTempo` → 120.0, `channels.channelCount` → 2, etc.) puis charge `fl_controller/device_FLStudioMCP.py` via `importlib` ;
- `FakeScore`, `FakeNote` pour `flpianoroll`, permettant d'importer `ComposeWithLLM.pyscript` (chargé comme source, extension non standard).

Tests à écrire :

| Fichier | Vérifie |
|---|---|
| `test_port_selection.py` | variable exacte, sous-chaîne, absente → erreur ; indices IAC/loopMIDI/Midi Through ; port matériel seul (« Arturia KeyStep 32 », « FLkey 37 ») → erreur, jamais choisi |
| `test_request_id.py` | réponse avec mauvais `id` ignorée puis bonne réponse acceptée ; réponse sans `id` → délai avec message « contrôleur périmé » ; fichier de commande contient `id` ; écriture via `.tmp` + `replace` |
| `test_connection_lock.py` | deux `send_command` en threads ne s'entrelacent pas (le faux FL répond à un seul fichier de commande à la fois et vérifie qu'il n'est pas réécrit pendant son traitement) |
| `test_controller.py` | dispatch recopie `id` ; `transport.setTempo` appelle `processRECEvent` avec `REC_Tempo`, `bpm*1000`, drapeaux attendus, et relit ; `project.snapshot` assemble les cinq blocs et isole une section en erreur |
| `test_tempo_tools.py` | `fl_set_tempo` refuse 5 et 600 ; `applied` faux → `ToolError` avec les deux valeurs |
| `test_trigger_not_running.py` | `is_fl_running` faux → `activate_window` et `trigger` retournent `False` sans appeler `osascript` ; `_run_pr_script` produit le message « FL Studio n'est pas lancé » |
| `test_piano_roll_lock.py` | deux `fl_send_notes` concurrents : la file de requêtes ne contient jamais les deux lots mélangés |
| `test_errors.py` | `unwrap` lève `ToolError` sur `success: false` ; un outil de chaque module lève `ToolError` au lieu de retourner « Error: » |

Test manuel documenté dans `docs/superpowers/specs/manual-checks.md` : FL Studio réel, `fl_set_tempo(128)` puis relecture dans la barre de transport ; `fl_get_project_snapshot` sur un projet avec trois canaux et deux pistes mixer nommées ; `fl_send_notes` avec FL Studio fermé → erreur sans lancement.

Commandes : `uv run pytest -q`, `uv run ruff check src fl_controller tests`.

## 9. Installateur et documentation

`install.sh`, `scripts/setup.sh`, `scripts/install_mcp_for_claude.sh` :

- **uv absolu** : `UV_BIN="$(command -v uv)"` après installation ; la configuration Claude Desktop reçoit ce chemin. Les applications graphiques macOS n'ont ni `~/.local/bin` ni Homebrew dans leur `PATH`.
- **Claude Code** : si `claude` est disponible, `claude mcp add --scope user fl-studio -- "$UV_BIN" run --directory "$PROJECT_DIR" fl-studio-mcp` ; si la commande refuse (politique d'entreprise) ou si `claude` est absent, afficher la configuration manuelle. Plus jamais d'écriture directe dans `~/.claude.json`.
- **Détection IAC** : remplacer `system_profiler SPAudioDataType` (périphériques audio, pas MIDI) par `uv run python -c "import mido; print(mido.get_output_names())"` après `uv sync`, et chercher « IAC » dans la liste. Message adapté si aucun port.
- **Mode non interactif** : options `--client desktop|code|both|none` et `--yes` sur `install.sh`, transmises aux scripts ; sans `--yes`, comportement interactif inchangé.
- **README** : section configuration (variables `FL_STUDIO_MCP_MIDI_PORT`, `FL_STUDIO_MCP_SETTINGS_DIR`), erreurs MCP, tempo, snapshot, note sur le nom localisé du bus IAC (« Gestionnaire IAC Bus 1 » en français), note « FL Studio doit être lancé » pour le piano roll, `install.ps1` non mis à jour.
- `pyproject.toml` : version `0.2.0`, et `FastMCP(name=..., version=...)` pour ne plus annoncer la version de FastMCP comme version du serveur.

## 10. Ordre de réalisation

1. `tests/fakes.py` et chargement du contrôleur sous pytest (prérequis de tout le reste).
2. Sélection stricte du port.
3. Identifiant de requête, verrou, écriture atomique serveur, contrôleur qui recopie `id`.
4. Erreurs uniformes (`errors.py`, migration des outils, adaptation des tests PR #5).
5. Piano roll : `is_fl_running`, verrou, message.
6. Tempo.
7. Snapshot.
8. Installateur, README, version.
9. Branches et PR upstream (trois).

Chaque étape : tests d'abord, implémentation, `pytest` et `ruff` verts, commit.
