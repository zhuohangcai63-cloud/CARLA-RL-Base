# Mixed long-tail template. Runtime compatibility checks decide which event can spawn.

param scenario_name = "mixed_long_tail"
param weather = Uniform("ClearNoon", "CloudyNoon", "WetNoon", "MidRainyNoon")
param traffic_light_policy = "on"

param ego_spawn_index = Range(0, 1000000)
param destination_spawn_index = Range(0, 1000000)
param min_route_distance = Range(70, 160)

param number_of_vehicles = Range(4, 14)
param number_of_walkers = Range(0, 12)
param pedestrian_cross_factor = Range(0.0, 0.35)

param long_tail_event_type = "mixed_long_tail"
param direction = Uniform("auto", "left", "right")
param event_distance_min = Range(30, 40)
param event_distance_max = Range(55, 80)
param trigger_distance_min = Range(8, 12)
param trigger_distance_max = Range(14, 24)
param enabled_prob = 1.0
param allow_skip_if_incompatible = True
