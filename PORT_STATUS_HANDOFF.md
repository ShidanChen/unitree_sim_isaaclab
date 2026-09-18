# Isaac Sim 6.1 / Isaac Lab 3.0 Port — Status Handoff

Date: 2026-09-18
Goal: get the G1 Dex1 Meta Quest teleoperation stack (`core_unitree_sim_isaaclab_main`) running on Isaac Sim 6.1 + Isaac Lab 3.0, since the original setup only supports Isaac Sim 5.1 and 5.1 crashes on this machine's Blackwell GPU + driver.

## What's installed

- **Isaac Sim 6.1.0** + **Isaac Lab 3.0.0** (officially matched versions) in a fresh venv: `/home/digit/isaac-sim-6.1/venv` (Python 3.12).
- Isaac Lab 3.0 source checked out at `/home/digit/dev/isaac/IsaacLab-3.0` (branch `release/3.0.0`), installed editable into that venv.
- A **working copy** of the teleop project was made at:
  `/home/digit/dev/isaac/core_unitree_sim_isaaclab_main_isaac61`
  (copied from `/home/digit/dev/isaac/core_unitree_sim_isaaclab_main`, which is untouched/still on 5.1).
  **All fixes below are in the `_isaac61` copy only.** They have NOT been applied to the original checkout.

## Why 6.1 instead of 5.1

Isaac Sim 5.1 segfaults on this machine (`librtx.scenedb.plugin.so` crash in `carbOnPluginStartup`) — a known NVIDIA issue: driver 595.84 isn't validated for Isaac Sim 5.1's RTX plugin on Blackwell (`sm_120`) GPUs. Confirmed via GitHub issues #619/#643 and NVIDIA dev forums. Isaac Sim 6.1 boots cleanly on this same driver/GPU.

## Fixes applied to get `sim_main.py` running on Isaac Lab 3.0 (all in `core_unitree_sim_isaaclab_main_isaac61`)

1. **`--headless` CLI flag removed** by Isaac Lab 3.0 — AppLauncher no longer registers it. Fix: use `HEADLESS=1` env var instead. (`sim_main.py` didn't need code changes, just invocation.)

2. **`--enable_cameras` CLI flag removed** too. Fix: in `sim_main.py`, after argparse, force it on via the args namespace since AppLauncher reads it as a plain attribute:
   ```python
   if not hasattr(args_cli, "enable_cameras"):
       args_cli.enable_cameras = True
   ```

3. **No GUI window without `--visualizer kit`** — Isaac Lab 3.0 defaults to headless unless a Kit visualizer is explicitly requested. Fix: pass `--visualizer kit` on the command line when you want a window.

4. **`isaaclab.utils.assets.check_usd_path_with_timeout` removed**, replaced by `check_file_path` (same boolean-ish semantics: 0 = not found). Fixed in:
   - `tasks/common_scene/base_scene_randomized_pickplace_cfg.py`
   - `isaac-projects/room_randomizer_lab/room_scene_cfg.py`

5. **`SimulationCfg.physx` field renamed to `SimulationCfg.physics`**, now typed `PhysicsCfg | None` (default `None`, resolved internally to a `PhysxCfg` if `None`). Since these task files write `self.sim.physx.X = value` directly, each needed:
   ```python
   from isaaclab_physx.physics import PhysxCfg
   ...
   if self.sim.physics is None:
       self.sim.physics = PhysxCfg()
   self.sim.physics.X = value   # was self.sim.physx.X
   ```
   Applied to **18 task config files** under `tasks/g1_tasks/` and `tasks/h1-2_tasks/`, plus the runtime accesses in `sim_main.py` (`env.sim.cfg.physics.X`, was `env.sim.physx.X`).

6. **PhysX-backend asset data (`joint_pos`, `joint_vel`, `applied_torque`, etc.) now returns a `ProxyArray`** (Warp-array-backed wrapper), not a plain `torch.Tensor`. Its `.device`/`.dtype` properties return raw Warp objects, not `torch.device`/torch dtype, which breaks `torch.tensor(..., device=..., dtype=...)` calls. Fix: read `.torch` (a property, zero-copy view) instead of the raw ProxyArray. Applied to 5 observation files:
   - `tasks/common_observations/g1_29dof_state.py`
   - `tasks/common_observations/dex3_state.py`
   - `tasks/common_observations/inspire_state.py`
   - `tasks/common_observations/gripper_state.py`
   - `tasks/common_observations/h12_27dof_state.py`

   Indexing/arithmetic/`.clone()` on ProxyArray generally work fine as-is (it implements `__torch_function__`), so this pattern (`.device`/`.dtype` read into a raw torch constructor call) is the specific thing to search for if more breakage surfaces in files I didn't touch (there are ~20 more files touching `.data.root_pos_w`, `.data.body_pos_w` etc. that were NOT confirmed broken, just not yet exercised at runtime — see "Not yet verified" below).

   **Also flagged but not yet audited**: Isaac Lab 3.0 changed the quaternion convention from `(w,x,y,z)` to `(x,y,z,w)`. Any code reading `root_quat_w`/`body_quat_w` and assuming the old order will silently produce wrong rotations (no crash). Not confirmed as an actual problem yet, but worth grepping for quaternion component indexing (`quat[0]` as w, etc.) across the task/event/reward files.

7. **`teleimager` submodule's `pyproject.toml` pinned `requires-python = ">=3.8,<3.12"`**, blocking pip install on our Python-3.12 venv. Bumped to `<3.13` in:
   `/home/digit/dev/isaac/core_unitree_sim_isaaclab_main_isaac61/teleimager/pyproject.toml`
   Then `pip install -e teleimager --no-deps` into the 6.1 venv.

8. **Interactive Kit viewport never showed the scene** (blank sky) even though offscreen sensor cameras worked fine. Root cause: Isaac Lab's SimulationContext manages the sensor cameras' render products separately from the GUI's own interactive viewport camera, and nothing ever pointed the interactive camera at the scene. Fix: added an explicit camera-framing call in `sim_main.py` right after env creation:
   ```python
   if not getattr(args_cli, "headless", False):
       try:
           robot_pos = env.scene["robot"].data.default_root_state[0, :3].tolist()
           eye = (...)      # currently being tuned, see below
           target = (...)
           env.sim.set_camera_view(eye=eye, target=target)
       except Exception as e:
           print(f"[sim] failed to frame viewport camera: {e}")
   ```
   This is cosmetic only (doesn't affect the real teleop camera feed, which goes over ZMQ to the Quest headset, not through this on-screen viewport) but useful for local debugging/screenshots. **The exact eye/target offset is still being iterated on** — I was mid-adjustment (side-view for checking robot standing posture) when this handoff was requested. Feel free to just delete this block if it's not wanted; it's not required for functionality.

9. **Real, hard-to-diagnose bug: DDS command/state channel completely broken** (`cyclonedds.core.DDSException: [DDS_RETCODE_PRECONDITION_NOT_MET]` on *every* `dds_create_topic` call, for any topic name/domain/interface). Root cause, found after extensive isolation (see below): **version mismatch between the pinned Python `cyclonedds==0.10.2` binding and the locally-compiled C library** (`/home/digit/dev/isaac/cyclonedds/install`, which is at commit `0.10.5-5-g76360fb7`, i.e. 5 commits past the 0.10.5 tag — several point releases ahead of what 0.10.2's Python bindings expect). This is NOT specific to Isaac Sim 6.1 — it reproduces identically on the old, previously-working Isaac Sim 5.1 / `unitree_sim_env`, so it's a pre-existing environment problem that happened to surface today.

   **Fix**: install the real prebuilt PyPI wheel `cyclonedds==11.0.1` (self-contained, properly matched Python+C build — no local library needed) instead of building `0.10.2` from source against the mismatched local library:
   ```bash
   pip install "cyclonedds==11.0.1" --force-reinstall --only-binary=:all:
   ```
   Applied to **both**:
   - `/home/digit/isaac-sim-6.1/venv` (the sim side)
   - `/home/digit/miniconda3/envs/tv_teleop` (the XR bridge side — both sides must speak the same DDS wire version or you get `ddsi_xt_type_init_impl with invalid type object` errors)

   `pip` will complain `unitree-sdk2py 1.0.1 requires cyclonedds==0.10.2, but you have cyclonedds 11.0.1` — this is a harmless metadata warning; the actual runtime works fine (verified `ChannelPublisher`/`ChannelSubscriber` creation with real `LowState_`/`LowCmd_` types after the upgrade). Did **not** edit `unitree_sdk2_python/setup.py`'s pin, since that's a shared submodule also used by the still-untouched original 5.1 environment (`unitree_sim_env`, which still has `0.10.2` installed and works there — untouched, don't "fix" it without separately verifying 5.1 needs it).

   **Earlier, unrelated CycloneDDS fix from a previous session** (worth knowing about, not something I did today, don't re-break it): `unitree_sdk2_python/unitree_sdk2py/core/channel_config.py`'s `ChannelConfigHasInterface` XML template originally had a `<Tracing><OutputFile>/tmp/cdds.LOG</OutputFile></Tracing>` block that caused a **buffer overflow crash** on `ChannelFactoryInitialize(id, interface)` — unrelated to the 11.0.1 fix above (that one crashed with SIGABRT before ever reaching Topic creation; this one raised a clean Python exception). The Tracing block was removed from that file. If you ever see `*** buffer overflow detected ***: terminated` again on `ChannelFactoryInitialize`, check that file — someone may have re-added tracing.

## Confirmed working (via `Isaac-PickPlace-RedBlock-G129-Dex1-Joint` task)

- Full env creation: scene, physics, robot, room-randomization event.
- All 3 sensor cameras (head/front, left wrist, right wrist) report ready and stream over ZMQ.
- DDS publisher (`rt/lowstate`) and subscriber (`rt/lowcmd`) both initialize with zero errors (previously every step spammed `'G1RobotDDS' object has no attribute 'publisher'` etc. — now silent/clean).
- Control loop runs to completion (`--max_steps N; stopping cleanly`) with no exceptions.
- XR bridge (`xr_teleoperate/teleop/teleop_hand_and_arm.py`, `tv_teleop` conda env) connects to the sim's image server, loads the G1 IK model, subscribes to all DDS topics successfully, and reaches the "press r to start syncing" prompt. Was about to test with a real Meta Quest headset connection when this task was interrupted.

## RESOLVED: "object positions wrong" / "robot didn't stand properly" (found and fixed after this doc was first written)

This turned out to be a **second, systemic Isaac Lab 3.0 breaking change**, separate from and just as significant as the `.physx`→`.physics` rename:

**Root cause**: Isaac Lab 3.0 changed the quaternion component order for *every* pose field from **`(w, x, y, z)`** to **`(x, y, z, w)`**. Confirmed directly in source:
`IsaacLab-3.0/source/isaaclab/isaaclab/assets/asset_base_cfg.py`:
```python
rot: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 1.0)
"""Quaternion rotation (x, y, z, w) of the root in simulation world frame."""
```
(Identity used to be `(1,0,0,0)`, now `(0,0,0,1)`.) Same change applies to `CameraCfg.OffsetCfg.rot`. Our entire ported codebase still authored/consumed quaternions in the old `(w,x,y,z)` order (that's what upstream/Isaac Lab 2.3.2/5.1 use), so every authored rotation, every yaw-to-quaternion conversion for room randomization, and every quaternion-to-yaw extraction was silently getting the wrong answer — hence robots/objects spawning rotated wrong, gripper hands ending up jammed into containers at bad angles instead of reaching naturally, and the room-randomization placement retry loop behaving erratically (one run hung for 5+ minutes; after the fix it completes in seconds).

**Visual confirmation**: before the fix, the robot's own head camera showed both Dex1 hands closed into fists and wedged sideways into storage bins. After the fix, same camera shows both hands open, hovering naturally above the table next to three cleanly-placed red/blue/yellow cubes — matching the project's own reference screenshot (`img/pickplace_redblock_g129_dex1.png`) almost exactly.

### Fix applied (all in `core_unitree_sim_isaaclab_main_isaac61`)

**1. Static authored quaternion tuples** — reordered mechanically via a one-off script (`old (w,x,y,z)` → `new (x,y,z,w)`, i.e. move the first element to the end). 42 tuples reordered automatically across:
- `isaac-projects/room_randomizer_lab/room_scene_cfg.py`
- `tasks/common_scene/base_scene_randomized_pickplace_cfg.py`, `base_scene_pickplace_cylindercfg.py`, `base_scene_pick_redblock_into_drawer.py`
- `tasks/common_config/camera_configs.py` (camera offset `rot_offset=` tuples)
- All 15 `tasks/g1_tasks/**/*_env_cfg.py` and `tasks/h1-2_tasks/**/*_env_cfg.py` files with `rot=(...)` / `init_rot=(...)` (robot spawn rotation, object spawn rotation)

Plus 2 **function-default-value** tuples the script's regex missed (type-annotated params, e.g. `init_rot: Tuple[float,...] = (0.7071, 0, 0, 0.7071)`), fixed by hand via `sed` in:
- `tasks/common_config/robot_configs.py` (8 identical occurrences, all `G1RobotPresets.*` factory function defaults)
- `tasks/common_config/camera_configs.py` (1 occurrence)

**If you add new task files or new camera/robot presets later, remember: any literal 4-tuple passed to a `rot=`/`init_rot=`/`rot_offset=` keyword must be authored in `(x,y,z,w)` order now, not `(w,x,y,z)`.**

**2. Dynamic quaternion math** — functions that read `root_quat_w`/`body_quat_w`/`default_root_state[3:7]` or construct quaternions to write back via `write_root_state_to_sim`:
- `tasks/utils/room_randomizer/room_events.py` — `_quat_wxyz_yaw(quat)` was unpacking `w, x, y, z = quat`; fixed to `x, y, z, w = quat` (the trig formula itself was already correct, only the unpacking order was wrong).
- `tasks/utils/room_randomizer/placement_utils.py` — `build_root_state()`'s internal `yaw_to_quat()`/quaternion-multiply math is self-consistently `(w,x,y,z)` internally (didn't need touching), but the final `state[:, 3:7] = quat` assignment needed a reorder before writing into the Isaac-Lab-3.0-shaped state tensor: changed to `state[:, 3:7] = quat[:, [1, 2, 3, 0]]`.
- `tasks/common_observations/g1_29dof_state.py` and `h12_27dof_state.py` — both had an existing `ensure_quat_w_first(quat, assume_w_first=True)` helper call (for IMU data) that hardcoded the wrong assumption; changed to `assume_w_first=False`.

**Not yet audited** (lower priority — not on the redblock task's import path, but will have the same bug if/when exercised):
- `tasks/g1_tasks/pickplace_medicine_bottle_hospital_g1_29dof_dex1/pickplace_medicine_bottle_hospital_g1_29dof_dex1_joint_env_cfg.py` — has its own `_yaw_from_quaternion_wxyz` helper, same class of bug likely.
- `tasks/g1_tasks/pickplace_medicine_bottle_hospital_g1_29dof_dex1/mdp/container_goal.py` — uses `root_quat_w`/`quaternion_apply` for the hospital task's basket-alignment logic.
- `tasks/g1_tasks/pick_place_cylinder_g1_29dof_dex1/pickplace_cylinder_g1_29dof_dex1_joint_env_cfg.py` — uses `body_quat_w`/`quat_apply` for Ridgeback-relative hand tracking (wholebody cylinder task).
- `tools/validate_dex1_grasp_contacts.py` — standalone debug/validation tool, uses `body_quat_w`, `quat_apply`.
- General method: grep each file for `quat_w`, `quat_apply`, or any place that does `quat[0]`/`w, x, y, z = ...`-style unpacking, and check whether the tensor came from Isaac-Lab-3.0-native data (needs `x,y,z,w` handling) or from a purely-internal self-consistent computation (may not need touching — check both ends: where values come from and where they're written back to).

### How to open a bare Isaac Sim window instead of "Isaac Lab" branding

Isaac Lab's `AppLauncher` loads a Kit "experience" file that determines the window title/menu chrome. Its default experience is Isaac-Lab-branded ("Isaac Lab 3.0.0" in the title bar, minimal menu). To get the native Isaac Sim look and full menu bar (File/Edit/Create/Tools/Utilities/Layouts, Robot Inspector panel, etc.), pass:
```bash
--experience isaacsim.exp.full.kit
```
This resolves relative to Isaac Sim's own `apps/` folder inside the venv (`.../site-packages/isaacsim/apps/isaacsim.exp.full.kit`, which declares `title = "Isaac Sim Full"`). Verified this doesn't break anything — task/DDS/camera functionality all still worked identically, just with the native branding and a heavier extension set (slower boot, ~2-3x more CPU during startup than the lean Isaac Lab experience). One new harmless warning appears with this experience: `ROS2 Bridge startup failed ... libament_index_cpp.so: cannot open shared object file` — we don't use ROS2 for this project, so this can be ignored. Other `.kit` files are available in the same `apps/` folder if a different look/extension set is ever wanted (e.g. `isaacsim.exp.base.kit` for a lighter non-Lab option, `isaacsim.exp.full.streaming.kit` for livestream-oriented builds).

## NOT yet verified / open items

1. **DDS fix not yet validated with a real Quest headset.** Everything up to the XR bridge's "press r" prompt is confirmed; actually moving the robot via headset tracking has not been tested. (This was in progress when the quaternion bug was discovered and took priority — the XR bridge should be relaunched to reconnect after any further sim restarts, since it needs a fresh connection to the sim's image server each time the sim process restarts.)

2. **Only one task tested** (`Isaac-PickPlace-RedBlock-G129-Dex1-Joint`). The other task config files got the same mechanical `.physx`→`.physics` and quaternion-tuple fixes but weren't individually run — worth spot-checking at least one wholebody/hospital task since those pull in the "not yet audited" dynamic-quaternion files listed above.

3. **Only the `_isaac61` copy is fixed.** `core_unitree_sim_isaaclab_main` (original) is untouched and still targets 5.1 — decide whether to eventually replace it or keep both.

4. **Room randomization occasionally still slow.** One run's `env.reset()` hung for 5+ minutes before the quaternion fix; after the fix, resets have been consistently fast (seconds), but this was only observed across a handful of runs — worth running the project's own recommended "100 consecutive resets" check (mentioned in their docs) before fully trusting it.

## How to run it

```bash
cd /home/digit/dev/isaac/core_unitree_sim_isaaclab_main_isaac61
export OMNI_KIT_ACCEPT_EULA=YES
export UNITREE_DDS_NETWORK_INTERFACE=eno2   # or 'lo' for local-only testing, no real Quest
export DISPLAY=:12.0                         # only needed for GUI
/home/digit/isaac-sim-6.1/venv/bin/python sim_main.py \
  --device cuda:0 --visualizer kit --meta_quest \
  --task Isaac-PickPlace-RedBlock-G129-Dex1-Joint
```

XR bridge (separate terminal, needs a real PTY/tmux for the `r`/`q` keyboard listener to work):
```bash
source /home/digit/miniconda3/etc/profile.d/conda.sh
conda activate tv_teleop
export CYCLONEDDS_HOME=/home/digit/dev/isaac/cyclonedds/install
export LD_LIBRARY_PATH=/home/digit/dev/isaac/cyclonedds/install/lib:${LD_LIBRARY_PATH:-}
export DDS_DOMAIN_ID=1
cd /home/digit/dev/xr_G1/xr_teleoperate/teleop
python -u teleop_hand_and_arm.py --input-mode=controller --arm=G1_29 --ee=dex1 --sim \
  --img-server-ip=139.174.67.90 --network-interface=eno2
```
Quest URL: `https://139.174.67.90:8012/?ws=wss://139.174.67.90:8012`

## Useful log files from today's testing (all under `/home/digit/isaac-sim-6.1/`)

- `install.log` — Isaac Sim 6.1.0 pip install
- `isaaclab_install2.log` — Isaac Lab 3.0.0 install
- `sim_main_port_test*.log` — sequential smoke-test iterations (test5 onward show the fixes landing one by one; test15+ show a fully clean run)
- `sim_main_quest_test*.log` — meta_quest-mode runs with real network interface
- `xr_bridge_tmux*.log` — XR bridge logs
