from pathlib import Path

import pytest

from box2robot_gpu_worker.worker import TrainingWorker, _policy_expects_images


def _make_checkpoint(model_dir: Path, step: int, *, padded: bool = True) -> Path:
    step_dir = f"{step:06d}" if padded else str(step)
    ckpt_dir = model_dir / "checkpoints" / step_dir / "pretrained_model"
    ckpt_dir.mkdir(parents=True, exist_ok=True)
    (ckpt_dir / "train_config.json").write_text("{}", encoding="utf-8")
    return ckpt_dir.parent


def _make_worker(tmp_path: Path) -> TrainingWorker:
    return TrainingWorker("http://example.com", output_dir=str(tmp_path / "outputs"))


def test_append_resume_args_uses_train_config_path(tmp_path: Path) -> None:
    model_dir = tmp_path / "model"
    ckpt_dir = _make_checkpoint(model_dir, 10_000)
    cmd = ["python", "-m", "lerobot.scripts.lerobot_train", "--steps=30000"]

    resolved = TrainingWorker._append_resume_args(
        cmd,
        model_dir=str(model_dir),
        resume_from_step=10_000,
        train_steps=30_000,
    )

    assert resolved == ckpt_dir
    assert "--resume=true" in cmd
    assert f"--config_path={ckpt_dir / 'pretrained_model' / 'train_config.json'}" in cmd
    assert not any(arg.startswith("--checkpoint_path=") for arg in cmd)


def test_resolve_resume_checkpoint_supports_legacy_unpadded_dir(tmp_path: Path) -> None:
    model_dir = tmp_path / "model"
    ckpt_dir = _make_checkpoint(model_dir, 10_000, padded=False)

    resolved_ckpt_dir, config_path = TrainingWorker._resolve_resume_checkpoint(
        str(model_dir), 10_000
    )

    assert resolved_ckpt_dir == ckpt_dir
    assert config_path == ckpt_dir / "pretrained_model" / "train_config.json"


def test_resolve_effective_resume_step_falls_back_to_custom_params(tmp_path: Path) -> None:
    worker = _make_worker(tmp_path)

    step, source = worker._resolve_effective_resume_step(None, {"resume_from_step": "10000"})

    assert step == 10_000
    assert source == "custom_params.resume_from_step"


def test_resolve_effective_policy_type_switches_diffusion_to_transformer(tmp_path: Path) -> None:
    worker = _make_worker(tmp_path)

    policy_type = worker._resolve_effective_policy_type("diffusion", {"use_transformer": "true"})

    assert policy_type == "diffusion_transformer"


def test_resolve_effective_policy_type_accepts_transformer_backend_alias(tmp_path: Path) -> None:
    worker = _make_worker(tmp_path)

    policy_type = worker._resolve_effective_policy_type(
        "diffusion",
        {"diffusion_backbone": "transformer"},
    )

    assert policy_type == "diffusion_transformer"


def test_resolve_effective_policy_type_keeps_builtin_diffusion_by_default(tmp_path: Path) -> None:
    worker = _make_worker(tmp_path)

    policy_type = worker._resolve_effective_policy_type("diffusion", {})

    assert policy_type == "diffusion"


def test_resolve_resume_model_dir_supports_resume_job_id(tmp_path: Path) -> None:
    worker = _make_worker(tmp_path)
    resume_model_dir = tmp_path / "outputs" / "old_job" / "model"
    _make_checkpoint(resume_model_dir, 10_000)

    resolved_dir, source = worker._resolve_resume_model_dir(
        current_model_dir=str(tmp_path / "outputs" / "new_job" / "model"),
        custom_params={"resume_job_id": "old_job"},
    )

    assert resolved_dir == str(resume_model_dir.resolve())
    assert source == "custom_params.resume_job_id"


def test_resolve_resume_model_dir_supports_train_config_path(tmp_path: Path) -> None:
    worker = _make_worker(tmp_path)
    resume_model_dir = tmp_path / "outputs" / "old_job" / "model"
    ckpt_dir = _make_checkpoint(resume_model_dir, 10_000)
    config_path = ckpt_dir / "pretrained_model" / "train_config.json"

    resolved_dir, source = worker._resolve_resume_model_dir(
        current_model_dir=str(tmp_path / "outputs" / "new_job" / "model"),
        custom_params={"resume_model_path": str(config_path)},
    )

    assert resolved_dir == str(resume_model_dir.resolve())
    assert source == "custom_params.resume_model_path"


def test_append_resume_args_requires_target_steps_to_increase(tmp_path: Path) -> None:
    model_dir = tmp_path / "model"
    _make_checkpoint(model_dir, 10_000)
    cmd = []

    with pytest.raises(ValueError, match="目标步数必须大于 checkpoint 步数"):
        TrainingWorker._append_resume_args(
            cmd,
            model_dir=str(model_dir),
            resume_from_step=10_000,
            train_steps=10_000,
        )


def test_resolve_resume_checkpoint_fails_loudly_when_step_missing(tmp_path: Path) -> None:
    model_dir = tmp_path / "model"
    _make_checkpoint(model_dir, 10_000)

    with pytest.raises(FileNotFoundError, match=r"step=12000"):
        TrainingWorker._resolve_resume_checkpoint(str(model_dir), 12_000)


def test_policy_expects_images_detects_visual_input_feature() -> None:
    class DummyFeature:
        type = "VISUAL"

    class DummyConfig:
        input_features = {"observation.images.base_0_rgb": DummyFeature()}
        image_features = {}

    assert _policy_expects_images(DummyConfig()) is True


def test_policy_expects_images_handles_state_only_policy() -> None:
    class DummyFeature:
        type = "STATE"

    class DummyConfig:
        input_features = {"observation.state": DummyFeature()}
        image_features = {}

    assert _policy_expects_images(DummyConfig()) is False
