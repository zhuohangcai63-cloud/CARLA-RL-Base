# Scenic 采样模板：强调行人横穿交互强度。

# 场景名称，仅用于日志与统计标识。
param scenario_name = "pedestrian_crossing"
# 天气预设集合：每次 reset 从中随机选择一个。
param weather = Uniform("CloudyNoon", "WetNoon", "MidRainyNoon", "HardRainNoon")
# 交通灯策略："on"=正常红绿灯，"off"=环境侧常绿/冻结。
param traffic_light_policy = "on"

# Ego 出生点索引（超大范围随机，最终会对地图可用 spawn 点取模）。
param ego_spawn_index = Range(0, 1000000)
# 终点出生点索引（同上，用于规划目标路线）。
param destination_spawn_index = Range(0, 1000000)
# 最小路线长度阈值（米），越大越偏向长路线。
param min_route_distance = Range(30, 85)

# 背景车辆数量范围（每回合随机采样）。
param number_of_vehicles = Range(25, 50)
# 背景行人数量范围（每回合随机采样）。
param number_of_walkers = Range(0, 0)
# 全局行人横穿强度（0~1）：越接近 1 越容易随机横穿。
param pedestrian_cross_factor = Range(0.9, 1.0)
# 行人最小移动速度（m/s）。
param walker_speed_min = Range(1.8, 2.6)
# 行人最大移动速度（m/s），应 >= walker_speed_min。
param walker_speed_max = Range(2.8, 4.2)

# 强制前方横穿：单次触发时的目标人数上限（每次触发会在 [1,该值] 内随机）。
param forced_crossing_walkers = Range(1, 2)
# 单回合内强制横穿最多累计成功生成人数上限（防止负载过高）。
param forced_crossing_max_total = Range(18, 30)
# 强制横穿触发的前向最小距离（米，相对 ego 前方）。
param forced_crossing_distance_min = Range(20, 25)
# 强制横穿触发的前向最大距离（米，相对 ego 前方）。
param forced_crossing_distance_max = Range(25, 30)
# 强制横穿候选点的最小横向偏移（米，相对 ego 中线）。
param forced_crossing_lateral_min = Range(3.0, 5.0)
# 强制横穿候选点的最大横向偏移（米，相对 ego 中线）。
param forced_crossing_lateral_max = Range(8.0, 14.0)
# 行驶中每个 step 触发强制横穿的概率（0~1）。
param forced_crossing_trigger_prob = Range(0.005, 0.01)
# 两次强制横穿触发的最小间隔 step 数。
param forced_crossing_cooldown_steps = Range(200, 300)
# 回合开始后多少 step 内不触发强制横穿。
param forced_crossing_warmup_steps = Range(30, 70)
# Ego 速度低于该阈值（m/s）时，不触发强制横穿。
param forced_crossing_min_speed = Range(2.0, 3.0)
