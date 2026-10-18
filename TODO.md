# TODO

## 1re étape — Inventaire des opérations

- Parcourir les scripts de `examples/` et dresser la liste des constructeurs et opérations build123d utilisés.
- Pour chaque type d’opération, indiquer si son `operation_id` et l’origine de ses faces sont suivis au build.
- Compléter les suivis manquants, en commençant par les opérations présentes dans les exemples et non couvertes (Box, Pad, etc.).
- Ajouter un test ciblé par famille d’opérations suivie, qui vérifie les IDs et les rôles de face après le build.
- Vérifier le replay d’une modification manuelle après changement de dimensions du script.

## 2e étape — Outil MCP

- Ajouter une API qui retourne séparément les opérations issues du script et celles du journal manuel.
- Créer un outil MCP (`get_operations` ou `get_part_state`) qui expose ces deux listes sans confondre leurs origines.
- Pour chaque opération, exposer son ID, son type, ses paramètres et les données de récupération des inputs : sélecteur topologique et fallback géométrique pour les cibles manuelles; provenance disponible pour les opérations du script.
- Inclure les conflits ou cibles non résolues dans la réponse MCP avec un diagnostic exploitable.
- Tester l’API et l’outil MCP, puis vérifier un scénario de bout en bout : modifier le script, reconstruire la pièce et confirmer que les opérations manuelles sont toujours listées et rejouées.

## Déjà réalisé

- Centraliser le journal, la migration et la résolution des sélecteurs dans `workbench/manual_edits.py`.
- Résoudre les cibles topologiques avant le fallback géométrique et transmettre directement les objets `Face`/`Edge` trouvés aux opérations manuelles.
