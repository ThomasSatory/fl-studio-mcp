# Vérifications manuelles (FL Studio réel)

Prérequis : FL Studio lancé, contrôleur FLStudioMCP activé sur le bus IAC, serveur enregistré dans Claude Desktop.

1. `fl_get_tempo` → renvoie le tempo affiché dans la barre de transport.
2. `fl_set_tempo(128)` → la barre de transport affiche 128.000, l'outil renvoie `{"tempo": 128.0}`.
3. `fl_set_tempo(5)` → erreur « between 10 and 522 », tempo inchangé.
4. Projet avec trois canaux et deux pistes mixer nommées : `fl_get_project_snapshot` → les trois canaux, les deux pistes plus Master, le pattern courant.
5. Quitter FL Studio, `fl_send_notes([{"midi": 60, "duration": 1}])` → erreur « FL Studio is not running », FL Studio ne se lance pas.
6. Relancer FL Studio, ouvrir un piano roll, lancer ComposeWithLLM une fois, puis `fl_trigger_script` → la note C4 en attente apparaît.
