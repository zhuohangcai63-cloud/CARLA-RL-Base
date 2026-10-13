"""Opt-in real service regression; needs dedicated Scenic and p6 native workers."""
import json
import os
from pathlib import Path
import numpy as np
import pytest

pytestmark = [pytest.mark.carla, pytest.mark.skipif(
    not os.environ.get("EASYCARLA_FIX_INSTANCES"),
    reason="Set EASYCARLA_FIX_INSTANCES to dedicated Scenic,p6-Bench2Drive services")]


def test_real_physics_manifests_and_rejected_reset_keep_transitions():
    from easycarla_client import RemoteError
    from easycarla_client.vector_env import CarlaVectorEnv, BatchError
    instances = os.environ["EASYCARLA_FIX_INSTANCES"].split(",")
    assert len(instances) == 2
    env = CarlaVectorEnv(instances, timeout=180)
    report = {"real_carla": True, "status": "failed", "checks": {}}
    try:
        obs, info = env.reset(seed=[8, 8])
        before = obs["frame"].copy()
        descriptions = [single.client.experiment.describe_episode() for single in env.envs]
        assert [d["metadata"]["source"] for d in descriptions] == ["scenic", "bench2drive"]
        native_manifest = descriptions[1]["initial_manifest"]
        unavailable = [item for item in native_manifest["limitations"] if item.startswith("physics.")]
        assert unavailable, "native seed 8 must exercise disabled/inactive vehicle physics"
        assert any(actor["physics"] is not None for actor in native_manifest["actors"])
        report["native_manifest_unavailable_physics"] = unavailable
        report["checks"]["native_experiments_reset_without_unsafe_rpc"] = True
        for single in env.envs:
            observation = single.last_observation
            with pytest.raises(RemoteError) as caught:
                single.reset(options=[])
            assert caught.value.code == "invalid_request"
            assert caught.value.details["request_rejected"] is True
            assert single._state == "ready" and single.last_observation is observation
            with pytest.raises(RemoteError) as caught:
                single.client.reset(seed=True)
            assert caught.value.details["request_rejected"] is True
        with pytest.raises(BatchError) as caught:
            env.reset(options={"source": "unconfigured_source"})
        assert len(caught.value.failures) == 2 and caught.value.completed == {}
        assert all(error.details.get("request_rejected") for error in caught.value.failures.values())
        assert env.pool._failed == set()
        assert all(single._state == "ready" for single in env.envs)
        np.testing.assert_array_equal(env._stack()["frame"], before)
        assert [single.client.experiment.describe_episode() for single in env.envs] == descriptions
        report["checks"]["raw_gym_and_vector_rejections_preserve_episode"] = True
        for _ in range(3):
            obs, _, term, trunc, info = env.step(np.tile([0, 0, 1], (2, 1)))
            assert not term.any() and not trunc.any()
        np.testing.assert_array_equal(obs["frame"], before + 6)
        report["checks"]["valid_steps_after_rejection"] = True
        report.update(status="passed", initial_frames=before.tolist(), final_frames=obs["frame"].tolist())
    finally:
        try:
            env.close()
        finally:
            output = os.environ.get("EASYCARLA_FIX_REPORT")
            if output:
                path = Path(output)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(report, indent=2) + "\n")
