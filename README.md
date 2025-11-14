# road-topology-repro
## Highway-Based Topology Definition
This study defines adjacency between cities based on the existence of a direct highway (expressway) connection.
For each pair of cities (i, j), we use the Amap Driving Route Planning API to obtain:
- the shortest driving path
- total driving distance
- total duration
- percentage of distance traveled on highways (expressways)
A pair of cities is considered adjacent (Aij = 1) if:
    highway_ratio >= 0.6
    and
    the fastest route includes at least one expressway segment.
Otherwise, Aij = 0.
This rule ensures:
- adjacency reflects *actual high-speed transport connectivity*
- cities geographically distant can still be adjacent if directly linked by an expressway
- adjacency density aligns with real-world highway networks
