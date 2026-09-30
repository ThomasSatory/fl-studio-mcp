# Vérifications manuelles (FL Studio réel)

Prérequis : FL Studio lancé, contrôleur FLStudioMCP activé sur le bus IAC, serveur enregistré dans Claude Desktop.

1. `fl_get_tempo` → renvoie le tempo affiché dans la barre de transport.
2. `fl_set_tempo(128)` → la barre de transport affiche 128.000, l'outil renvoie `{"tempo": 128.0}`.
3. `fl_set_tempo(5)` → erreur « between 10 and 522 », tempo inchangé.
4. Projet avec trois canaux et deux pistes mixer nommées : `fl_get_project_snapshot` → les trois canaux, les deux pistes plus Master, le pattern courant.
5. Quitter FL Studio, `fl_send_notes([{"midi": 60, "duration": 1}])` → erreur « FL Studio is not running », FL Studio ne se lance pas.
6. Relancer FL Studio, ouvrir un piano roll, lancer ComposeWithLLM une fois, puis `fl_trigger_script` → la note C4 en attente apparaît.
7. Après `scripts/setup.sh`, piano roll ouvert : `fl_send_chord("Am7", octave=3)` → FL affiche A4, C5, E5, G5 (son étiquetage ; A3–G4 en convention C4 = 60) ; la réponse dit « 4 note(s) written ».
8. `fl_send_notes([{"note": "C4", "duration": 1, "slide": true}])` → la note apparaît en slide.
9. Recopier l'ancien `ComposeWithLLM.pyscript` (`git show main:scripts/ComposeWithLLM.pyscript`), le relancer une fois, refaire 8 → erreur « outdated … without pan/color/slide/porta ».
10. `fl_get_plugin_params` sur un canal vide → `{"valid": false, "params": []}`, pas d'erreur.
