import torch
import comfy.model_management
import comfy.conds
import comfy.hooks
from typing import TYPE_CHECKING, Dict, List
if TYPE_CHECKING:
    from comfy.model_patcher import ModelPatcher
    from comfy.model_base import BaseModel
    from comfy.controlnet import ControlBase

def prepare_mask(noise_mask, shape, device):
    """ensures noise mask is of proper dimensions"""
    noise_mask = torch.nn.functional.interpolate(noise_mask.reshape((-1, 1, noise_mask.shape[-2], noise_mask.shape[-1])), size=(shape[2], shape[3]), mode="bilinear")
    noise_mask = torch.cat([noise_mask] * shape[1], dim=1)
    noise_mask = comfy.utils.repeat_to_batch_size(noise_mask, shape[0])
    noise_mask = noise_mask.to(device)
    return noise_mask

def get_models_from_cond(cond, model_type):
    models = []
    for c in cond:
        if model_type in c:
            if isinstance(c[model_type], list):
                models += c[model_type]
            else:
                models += [c[model_type]]
    return models

def get_hooks_from_cond(cond, hooks_dict: Dict[comfy.hooks.EnumHookType, Dict[comfy.hooks.Hook, None]]):
    for c in cond:
        if 'hooks' in c:
            for hook in c['hooks'].hooks:
                hook: comfy.hooks.Hook
                with_type = hooks_dict.setdefault(hook.hook_type, {})
                with_type[hook] = None
    return hooks_dict

def convert_cond(cond):
    out = []
    for c in cond:
        temp = c[1].copy()
        model_conds = temp.get("model_conds", {})
        if c[0] is not None:
            model_conds["c_crossattn"] = comfy.conds.CONDCrossAttn(c[0]) #TODO: remove
            temp["cross_attn"] = c[0]
        temp["model_conds"] = model_conds
        out.append(temp)
    return out

def get_additional_models(conds, dtype):
    """loads additional models in conditioning"""
    cnets: List[ControlBase] = []
    gligen = []
    add_models = []
    hooks: Dict[comfy.hooks.EnumHookType, Dict[comfy.hooks.Hook, None]] = {}

    for k in conds:
        cnets += get_models_from_cond(conds[k], "control")
        gligen += get_models_from_cond(conds[k], "gligen")
        add_models += get_models_from_cond(conds[k], "additional_models")
        get_hooks_from_cond(conds[k], hooks)

    control_nets = set(cnets)

    inference_memory = 0
    control_models = []
    for m in control_nets:
        control_models += m.get_models()
        inference_memory += m.inference_memory_requirements(dtype)

    gligen = [x[1] for x in gligen]
    hook_models = [x.model for x in hooks.get(comfy.hooks.EnumHookType.AddModels, {}).keys()]
    models = control_models + gligen + add_models + hook_models

    return models, inference_memory

def cleanup_additional_models(models):
    """cleanup additional models that were loaded"""
    for m in models:
        if hasattr(m, 'cleanup'):
            m.cleanup()


def prepare_sampling(model: 'ModelPatcher', noise_shape, conds):
    device = model.load_device
    real_model: 'BaseModel' = None
    models, inference_memory = get_additional_models(conds, model.model_dtype())
    models += model.get_all_additional_models()  # TODO: does this require inference_memory update?
    memory_required = model.memory_required([noise_shape[0] * 2] + list(noise_shape[1:])) + inference_memory
    minimum_memory_required = model.memory_required([noise_shape[0]] + list(noise_shape[1:])) + inference_memory
    comfy.model_management.load_models_gpu([model] + models, memory_required=memory_required, minimum_memory_required=minimum_memory_required)
    real_model = model.model
    real_model.current_patcher = model

    return real_model, conds, models

def cleanup_models(conds, models):
    cleanup_additional_models(models)

    control_cleanup = []
    for k in conds:
        control_cleanup += get_models_from_cond(conds[k], "control")

    cleanup_additional_models(set(control_cleanup))

def prepare_model_patcher(model: 'ModelPatcher', conds):
    # check for hooks in conds - if not registered, see if can be applied
    hooks = {}
    for k in conds:
        get_hooks_from_cond(conds[k], hooks)
    model.register_all_hook_patches(hooks, comfy.hooks.EnumWeightTarget.Model)
