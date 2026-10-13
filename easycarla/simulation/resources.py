"""Only explicitly owned actors may be destroyed."""
class ActorRegistry:
    def __init__(self, on_forget=None):
        self.actors = {}
        self.roles = {}
        self.physics_enabled = {}
        self.on_forget = on_forget

    def forget(self, actor_id):
        """Drop an actor confirmed destroyed by its owning adapter."""
        self.actors.pop(actor_id, None)
        self.roles.pop(actor_id, None)
        self.physics_enabled.pop(actor_id, None)
        if self.on_forget is not None:
            self.on_forget(actor_id)

    def register(self, actor, role):
        if actor is None:
            raise RuntimeError(f"Actor spawn failed: {role}")
        if actor.id not in self.actors:
            self.actors[actor.id] = actor
            self.roles[actor.id] = str(role)
            self.physics_enabled.setdefault(actor.id, True)
        return actor

    def destroy(self, ids):
        failures = []
        for actor_id in list(dict.fromkeys(ids)):
            actor = self.actors.get(actor_id)
            if actor is None:
                continue
            try:
                if actor.is_alive:
                    if actor.type_id.startswith("sensor."):
                        actor.stop()
                    if actor.type_id.startswith("controller.ai.walker"):
                        actor.stop()
                # is_alive is snapshot based and is false for some fresh spawns.
                # Always send destruction for a registered actor, even before its
                # first physics tick. False + alive means CARLA refused cleanup.
                if actor.destroy() is False and actor.is_alive:
                    raise RuntimeError("CARLA refused actor destruction")
                self.forget(actor_id)
            except Exception as exc:
                failures.append({"actor_id": actor_id, "error": str(exc)})
        return failures

    def clear(self):
        # Controllers/sensors must be stopped before their parents disappear.
        ids = sorted(self.actors, key=lambda i: not self.actors[i].type_id.startswith(("sensor.", "controller.")))
        return self.destroy(ids)
