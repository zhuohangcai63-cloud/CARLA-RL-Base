# Bench2Drive-style parked vehicle opens door template.

param scenario_name = "vehicle_opens_door"
param weather = Uniform("ClearNoon", "CloudyNoon", "WetNoon")
param traffic_light_policy = "on"

param ego_spawn_index = Range(0, 1000000)
param destination_spawn_index = Range(0, 1000000)
param min_route_distance = Range(80, 160)

param number_of_vehicles = Range(4, 10)
param number_of_walkers = Range(0, 8)
param pedestrian_cross_factor = Range(0.0, 0.2)

param long_tail_event_type = "vehicle_opens_door"
param direction = Uniform("auto", "left", "right")
param event_distance_min = Range(35, 45)
param event_distance_max = Range(55, 70)
param trigger_distance_min = Range(8, 10)
param trigger_distance_max = Range(12, 16)
param reaction_time = Range(2.5, 3.5)
param hold_seconds = Range(4.0, 7.0)
param event_timeout_steps = Range(220, 320)
param allow_skip_if_incompatible = True
