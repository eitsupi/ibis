SELECT
  t0.*
FROM ibis_testing.batting AS t0
ANY JOIN ibis_testing.awards_players AS t1
  ON t0.playerID = t1.playerID