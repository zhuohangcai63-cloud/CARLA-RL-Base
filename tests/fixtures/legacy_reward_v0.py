"""Frozen pre-refactor reward reference; never imported by production code.

Extracted verbatim from the backed-up user working tree identified in
docs/audit/baseline.json; includes known old behavior intentionally.
"""
def legacy_reward(self, obs, done):
        reward = 0.0
        ego_state = obs['ego_state']
        speed = ego_state[3]
        acc_x = ego_state[5]
        acc_y = ego_state[6]
        front_dist = ego_state[7]
        lane_width, lateral_offset, angle_diff = obs['lane_info']

        # --- 核心约束逻辑 ---
        # 1. 角度约束：abs(angle_diff) < 0.25 (约30度)
        is_heading_aligned = abs(angle_diff) < 0.1

        # 2. 位置约束：lateral_offset < 1/3 车道宽
        is_centered = lateral_offset < (lane_width / 5.0)

        # 只有“姿态端正”才算合法的停车等待
        is_good_pose = is_heading_aligned and is_centered

        target_speed = self.desired_speed
        should_stop = False

        if 0 <= front_dist < 20.0:
            safe_speed = max(0, (front_dist - 2.0) * 0.5)
            target_speed = min(self.desired_speed, safe_speed)
            if front_dist < 2.0:
                should_stop = True

        if self.is_red_light_present:
            target_speed = 0.0
            should_stop = True

        if speed <= target_speed:
            reward += 1.0 * speed
        else:
            reward += -2.0 * (speed - target_speed)

        if target_speed > 1.0 and speed < 0.1:
            reward += -5.0

        reward += -1.0 * lateral_offset
        reward += -1.0 * abs(acc_y)

        if target_speed < self.desired_speed and speed <= target_speed and is_good_pose:
            reward += (self.desired_speed - target_speed)

        if self._is_collision:
            reward += -100.0
        if self._is_off_road:
            reward += -100.0

        ego_loc = self.ego.get_location()
        current_dist = ego_loc.distance(self.target_destination)
        dist_change = self.prev_dist_to_goal - current_dist

        if not should_stop:
            reward += dist_change * 5.0

        self.prev_dist_to_goal = current_dist

        if self.last_closest_idx > self.last_rewarded_idx:
            passed_waypoints = self.last_closest_idx - self.last_rewarded_idx
            reward_per_point = 10.0
            current_waypoint_reward = passed_waypoints * reward_per_point
            reward += current_waypoint_reward
            # print("经过路点")
            self.last_rewarded_idx = self.last_closest_idx

        if current_dist < 2.0:
            reward += 200.0
            print("到达终点")

        return reward / 50.0
