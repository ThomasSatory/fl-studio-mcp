# Écoute du mix — plan d'implémentation

Spec : `docs/superpowers/specs/2026-10-03-ecoute-du-mix-design.md`

## Global Constraints

- Python ≥ 3.10, style du dépôt (docstrings en anglais, messages d'erreur en anglais, `ToolError` de `fastmcp.exceptions` pour toute erreur visible par le modèle).
- numpy est déjà une dépendance (`numpy>=1.26`). Aucune autre dépendance nouvelle.
- Tests : `uv run pytest -q` ; lint : `uv run ruff check src tests`. Toute la suite doit rester verte.
- Ne jamais lancer le vrai FL Studio dans les tests.
- Insert flags : événement `236`, u32 little-endian à l'offset 4, bit Enabled = `0x08`. Routage : événement `235`, octets indexés par destination, non nul = route. Le N-ième 236 et le N-ième 235 décrivent l'insert N (0 = Master). Le dernier insert (index `insert_count - 1`, la piste « Current ») ne doit jamais être coupé.
- Commande de rendu macOS : `[binary, "-R", "-Ewav", str(flp)]` ; Windows : `[binary, "/R", "/Ewav", str(flp)]`. Sortie : `<stem du flp>.wav` dans le même dossier.
- Un commit par tâche, message en français au format du dépôt (`feat(rendu): …`), terminé par `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Task 1: Routage et coupure d'inserts dans `utils/flp.py`

Fichiers : `src/fl_studio_mcp/utils/flp.py` (ajouts), `tests/flp_builder.py` (helper), `tests/test_flp_mixer.py` (nouveau).

Ajouter à `flp.py` :

```python
EV_INSERT_ROUTING = 235
EV_INSERT_FLAGS = 236
INSERT_ENABLED = 0x08

def insert_count(project: Project) -> int:
    """Number of mixer inserts in the file (Master included), i.e. the 236 events."""

def mixer_routes(project: Project) -> dict[int, set[int]]:
    """insert index -> destination inserts, from the 235 events (in file order)."""

def disable_inserts(project: Project, indices) -> None:
    """Clear the Enabled bit of these inserts. FLPError for 0 (Master), for the last
    insert (FL 2025's 'Current' track: disabling it makes the render write nothing),
    for an index out of range, or when the file has no 236 events."""

def stem_keep_set(routes: dict[int, set[int]], target: int) -> set[int]:
    """{0, target} | inserts whose signal reaches target | inserts reached from target."""

def stem_project_bytes(data: bytes, target: int) -> bytes:
    """A copy of the project where every insert outside stem_keep_set(target) is disabled.
    Never touches 0 or the last insert. FLPError if target is 0, the last insert or out of range."""
```

Les valeurs d'événements sont des `bytes` dans `Event.value`. Modifier `value` en place (nouvel objet `bytes`), puis `project.to_bytes()`.

Dans `tests/flp_builder.py`, ajouter `add_mixer(project, routes: list[list[int]], flags: int = 0x4C)` qui ajoute, pour chaque insert i, un événement 236 (12 octets : 4 zéros, u32 flags, 4 zéros) suivi d'un 235 (octets de longueur max(dest)+1, 1 aux destinations ; `b"\x00"` si aucune route), à la fin des événements.

Tests (au minimum) :
- `mixer_routes` relit exactement les routes construites ; `insert_count` égal au nombre d'inserts.
- `stem_keep_set` sur le routage : 1→0, 2→0, 3→0, 4→1, 7→[1,8], 8→1, 19→3, 24→[2,19] (avec un dernier insert 26 sans route) : cible 7 → {0,7,1,8} ; cible 1 → {0,1,4,7,8} ; cible 3 → {0,3,19,24} (24 envoie dans 19, donc il est en amont de 3 ; 2 est en aval de 24 mais pas de 3, donc il n'est pas inclus).
- `disable_inserts` efface 0x08 sans toucher aux autres bits ; refuse 0, le dernier, un index hors limites.
- `stem_project_bytes` : relu, seuls les inserts hors de l'ensemble ont 0x08 effacé ; Master et dernier intacts ; aller-retour sans modification pour les inserts gardés.

## Task 2: Lancement du rendu `utils/render.py`

Fichiers : `src/fl_studio_mcp/utils/render.py` (nouveau), `tests/test_render.py` (nouveau).

```python
RENDER_KEEP = 30
MIN_TIMEOUT, MAX_TIMEOUT = 90.0, 1200.0

def find_fl_binary() -> Path
    # 1. env FL_STUDIO_MCP_FL_BINARY (must exist, else ToolError naming the variable)
    # 2. macOS: /Applications/FL Studio*.app/Contents/MacOS/OsxFL and ~/Applications/...,
    #    highest name in reverse sort order (FL Studio 2025 before FL Studio 21)
    # 3. Windows: C:\Program Files\Image-Line\FL Studio*\FL64.exe (same ordering)
    # else ToolError mentioning FL_STUDIO_MCP_FL_BINARY
def render_command(binary: Path, flp: Path, system: str | None = None) -> list[str]
def renders_dir() -> Path
    # env FL_STUDIO_MCP_RENDER_DIR; macOS ~/Library/Caches/fl-studio-mcp/renders;
    # Windows %LOCALAPPDATA%\fl-studio-mcp\renders; else ~/.cache/fl-studio-mcp/renders; mkdir parents
def render_timeout(song_seconds: float | None) -> float
    # 90 + 2*song_seconds clamped to [90, 1200]; None -> 600
def render_wav(flp_data: bytes, name: str, timeout: float, binary: Path | None = None) -> Path
```

`render_wav` : nom assaini (`[^A-Za-z0-9 _.-]` → `_`, 60 caractères max, non vide) ; sous un verrou inter-processus (fichier `renders_dir()/.render.lock`, `fcntl.flock` sur POSIX, `msvcrt.locking` sur Windows, plus un `threading.Lock` module) ; `tempfile.mkdtemp()` ; écrit `<name>.flp` ; `subprocess.Popen(render_command(...), stdout=DEVNULL, stderr=DEVNULL)` ; `wait(timeout)` → sur `TimeoutExpired` : `kill()`, `ToolError("FL Studio did not finish rendering within N s and was stopped. It may be waiting on a dialog (missing plugin, demo limitation); open the project in FL to check.")` ; ensuite `<name>.wav` absent → `ToolError` qui dit que FL n'a écrit aucun fichier (code de sortie inclus) ; sinon déplace le WAV dans `renders_dir()` sous `"<name> <YYYY-MM-DD HHhMMmSS>[-n].wav"` (unique), supprime le dossier temporaire (`shutil.rmtree` dans un `finally`), purge `renders_dir()` pour ne garder que les `RENDER_KEEP` WAV les plus récents, et renvoie le chemin.

Tests avec un faux exécutable : script Python exécutable (`#!<sys.executable>`) écrit dans `tmp_path`, qui lit le dernier argument et écrit un petit WAV `<stem>.wav` à côté (ou ne fait rien, ou dort, selon une variable d'environnement). Couvrir : succès (fichier déplacé, nommé, dossier temporaire supprimé), pas de fichier → ToolError, délai → ToolError et processus tué, purge à 30, `render_command` macOS/Windows, `find_fl_binary` via variable d'environnement (existante / inexistante), `render_timeout`. Utiliser `FL_STUDIO_MCP_RENDER_DIR` pointé sur `tmp_path`.

## Task 3: Analyse `utils/mix_analysis.py`

Fichiers : `src/fl_studio_mcp/utils/mix_analysis.py` (nouveau), `tests/test_mix_analysis.py` (nouveau).

```python
BANDS = (("sub", 0, 60), ("low", 60, 250), ("low_mid", 250, 2000),
         ("high_mid", 2000, 6000), ("air", 6000, None))

def load_wav(path: Path) -> tuple[int, np.ndarray]   # float32 array (frames, channels) in -1..1
def integrated_lufs(x: np.ndarray, rate: int) -> float | None
def short_term_lufs_max(x: np.ndarray, rate: int) -> float | None
def true_peak_dbfs(x: np.ndarray) -> float | None
def band_balance(mono: np.ndarray, rate: int) -> dict[str, float]   # percentages, sum ~100
def stereo_image(x: np.ndarray) -> dict | None   # {"width_pct", "correlation"}; None if mono
def analyze_segment(x: np.ndarray, rate: int) -> dict
def analyze_mix(path: Path, sections: list[dict] | None = None) -> dict
def compare_stems(stems: list[dict]) -> list[str]
```

- `load_wav` : parcours RIFF (chunks quelconques, JUNK inclus, alignement pair), format tag 1 (PCM 16/24/32 bits), 3 (float 32/64), 0xFFFE (lire le sous-format à l'offset 24 du chunk fmt) ; `ToolError` sinon. Taille de data tronquée au multiple de bloc.
- LUFS : ITU-R BS.1770-4. K-weighting par deux biquads dont les coefficients sont calculés pour n'importe quelle fréquence d'échantillonnage (pré-filtre shelving : f0=1681.974450955533, G=3.999843853973347 dB, Q=0.7071752369554196 ; passe-haut RLB : f0=38.13547087602444, Q=0.5003270373238773), blocs de 400 ms avec 75 % de recouvrement, porte absolue -70 LUFS puis relative -10 LU ; -0.691 + 10·log10(somme des moyennes de carrés par canal). Filtrage avec une implémentation numpy (boucle `lfilter` maison vectorisée par canal acceptable, ou forme directe en Python sur float64 si < 5 s pour 4 min de stéréo 48 kHz ; mesurer). Court terme : fenêtres de 3 s, pas de 1 s, sans porte, max. None si silence/trop court.
- True peak : suréchantillonnage x4 par interpolation sinc fenêtrée (noyau de 48 taps, fenêtre de Kaiser ou Hann) appliquée par `np.convolve` sur chaque canal, max avec la crête échantillon. dBFS arrondis à 0.1.
- `analyze_segment` renvoie : `peak_dbfs`, `true_peak_dbfs`, `rms_dbfs`, `lufs`, `crest_db`, `over_samples` (|x| > 1.0), `bands`, `stereo` (si stéréo). Les valeurs silencieuses sont `None`.
- `analyze_mix(path, sections)` : `sections` = liste de `{"name", "start_seconds", "end_seconds"}` (bornée à la durée, sections vides ignorées). Renvoie `{"path", "sample_rate", "channels", "duration_seconds", "overall": analyze_segment + "short_term_lufs_max", "silence": {"leading_seconds", "trailing_seconds"} (seuil -60 dBFS), "sections": [...] (si fourni : name, start/end_seconds, lufs, peak_dbfs, rms_dbfs, bands), "warnings": [...]}`. Fichier totalement silencieux → `overall` avec des None et l'avertissement « the render is silent ».
- Avertissements globaux (textes courts en anglais, chiffres inclus) : over_samples > 0 (« N samples above 0 dBFS: they clip when exported to 16/24-bit ») ; true peak > -1.0 dBTP ; LUFS > -7 (« very loud, likely over-limited ») ; LUFS < -18 (« quiet: streaming platforms normalise to about -14 LUFS ») ; crest_db < 6 ; corrélation < 0 (mono compatibility) ; sub+low > 65 % (« bass-heavy ») ; high_mid+air < 4 % (« dull, little top end ») ; silence de début > 2 s ; section dont la sonie s'écarte de plus de 8 LU de la médiane des sections non silencieuses (« section X is N LU quieter/louder than the rest »).
- `compare_stems(stems)` : chaque entrée `{"track", "name", "lufs", "bands", "silent"}` ; avertissements : stem silencieux (« track N (name) is silent in the render ») ; conflit dans le grave : deux stems non silencieux dont chacun a ≥ 35 % d'énergie dans la même bande `sub` ou `low` et des LUFS à moins de 6 LU l'un de l'autre (« kick and 808 both carry most of their energy in sub: they mask each other; sidechain or EQ one of them ») ; stem qui domine : LUFS au moins 3 LU au-dessus de tous les autres et part d'énergie > 60 % si `share_pct` est présent.

Tests avec des signaux synthétiques écrits en WAV (16 bits PCM, float32, chunk JUNK, extensible) : lecture correcte des formats ; sinus 1 kHz stéréo à -20 dBFS de crête → LUFS ≈ -23.0 ± 0.5 (référence BS.1770 : un sinus 1 kHz à 0 dBFS crête en stéréo ≈ -3.01 LUFS) ; sinus 50 Hz → `sub` dominant, 8 kHz → `air` dominant ; signal avec valeurs à 1.5 → `over_samples` et avertissement ; inter-sample peak : sinus à fs/4 déphasé de 45° avec crêtes échantillons à 0.707 → true peak ≈ 0 dBTP (± 0.5) ; L = -R → corrélation ≈ -1 ; sections : section 2 plus forte de 12 dB → avertissement ; silence en tête de 3 s détecté ; `compare_stems` conflit de sub et stem silencieux ; perf : 4 min de stéréo 48 kHz analysées en < 10 s (marquer le test et le garder rapide, ou l'omettre et mesurer dans le rapport).

## Task 4: Outil `fl_render_mix` et intégration

Fichiers : `src/fl_studio_mcp/utils/project_files.py` (ajout `current_project_file`), `src/fl_studio_mcp/tools/arrangement.py` (utiliser la fonction factorisée), `src/fl_studio_mcp/tools/render.py` (nouveau), `src/fl_studio_mcp/tools/__init__.py`, `src/fl_studio_mcp/server.py` (enregistrement + instructions), `src/fl_studio_mcp/tools/samples.py` (docstring de `fl_analyze_audio` qui renvoie vers `fl_render_mix`), `tests/test_tool_registry.py` (84 outils, annotation), `tests/test_render_tool.py` (nouveau), `README.md`, `pyproject.toml` et `src/fl_studio_mcp/__init__.py` (version `0.5.0`).

1. Déplacer `_current_file` de `register_arrangement_tools` vers `project_files.current_project_file(conn, save: bool, project_path: str | None) -> Path` (même comportement), et l'utiliser dans arrangement.
2. `tools/render.py` :
   - `MAX_STEMS = 8`
   - `song_seconds(project) -> float | None` : fin du dernier clip de l'arrangement courant (ticks) / ppq × 60 / tempo ; None si pas de clip ou pas de tempo.
   - `section_bounds(project) -> list[dict]` : marqueurs de l'arrangement courant triés → sections `{name, start_seconds, end_seconds, start_bar, end_bar}` jusqu'à la fin du morceau ; une section implicite « Intro » si le premier marqueur est après la mesure 1 ; sans marqueur, blocs de 8 mesures nommés `"bars 1-8"`. Utiliser `Timeline(project.ppq, project.bar_ticks)` et le tempo.
   - `resolve_stems(spec, routes, channel_targets: list[int], last_insert: int) -> list[int]` : None → [] ; liste d'entiers → validés (1 ≤ t < last_insert, sinon ToolError), sans doublons, ordre conservé ; `"all"` → inserts qui reçoivent un canal (≠ 0) ∪ bus, triés ; `"buses"` → inserts ≠ 0 qui ont au moins une entrée parmi les inserts utilisés, triés ; plus de `MAX_STEMS` → ToolError qui demande de découper les appels. Autre chaîne → ToolError.
   - Outil `fl_render_mix(stems=None, sections=True, save=True, project_path=None)` annoté `ACTION` (lecture seule du projet, mais il sauvegarde et lance un processus). Déroulé : `get_connection()`, `project_files.controller_info(conn)`, `path = project_files.current_project_file(conn, save, project_path)`, `data = path.read_bytes()`, `project = Project.parse(data)` (en cas de `FLPError` : rendre quand même le mix, sans sections ni stems, avec une note), `wav = render.render_wav(data, path.stem, render.render_timeout(song_seconds(project)))`, `analysis = mix_analysis.analyze_mix(wav, section_bounds(project) if sections else None)`. Pour les stems : exiger `project.to_bytes() == data` (sinon ToolError comme `_load_checked`) ; `routes = flp.mixer_routes(project)` ; canaux via `run(conn, "channels.getAll")` → `target_fx_track` ; noms via `run(conn, "mixer.getAllTracks", {"include_empty": True})` ; pour chaque cible : `render_wav(flp.stem_project_bytes(data, t), f"{path.stem} - stem {t} {name}", timeout)`, `analyze_mix(stem_wav)["overall"]`, `share_pct` = 100 × énergie RMS² du stem / somme des énergies des stems ; `silent` si `peak_dbfs is None`. Avertissement si des canaux sont routés sur l'insert 0 (« N channel(s) play straight into Master and are in every stem: <names> »). `stem_warnings = mix_analysis.compare_stems(...)`.
   - Réponse : `{"project", "render": {"path", "seconds"}, "analysis", "stems" (si demandé), "stem_warnings", "notes"}`. Les notes indiquent que le rendu porte sur le fichier sauvegardé, qu'une automation de tempo décale les temps des sections, et que les WAV restent dans `renders_dir()` (lecture avec `fl_preview_sample`).
   - Docstring de l'outil orientée modèle : quand l'utiliser (après une modification de mix ou d'arrangement, pour vérifier), coût (~20 s pour le mix, ~20 s par stem), ce qu'il faut regarder.
3. Enregistrer `register_render_tools` dans `tools/__init__.py` et `server.py`. Ajouter aux instructions du serveur un paragraphe « Listening to the mix ».
4. `test_tool_registry.py` : 84 outils, `fl_render_mix` dans l'ensemble ACTION.
5. `tests/test_render_tool.py` : `section_bounds` (avec marqueurs, sans marqueur, premier marqueur après la mesure 1), `song_seconds`, `resolve_stems` (liste valide, hors limites, doublons, "all", "buses", trop de stems, chaîne inconnue), et l'outil de bout en bout avec `render.render_wav` et `project_files.current_project_file` remplacés par monkeypatch (faux WAV synthétiques), connexion factice qui répond à `system.info`, `channels.getAll`, `mixer.getAllTracks`.
6. README : section « Listening to the mix » (outil, coût, où sont les WAV, variables `FL_STUDIO_MCP_FL_BINARY` et `FL_STUDIO_MCP_RENDER_DIR`, Windows non vérifié), ligne dans les tables d'outils, « Changed in 0.5.0 ». Version `0.5.0`.
