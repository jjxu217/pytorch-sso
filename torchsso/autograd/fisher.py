import torch
import torch.nn.functional as F
from torchsso.autograd.utils import disable_param_grad

FISHER_EXACT = 'fisher_exact'
FISHER_MC = 'fisher_mc'
FISHER_EMP = 'fisher_emp'


def fisher_for_cross_entropy(model, inputs, fisher_types, compute_emp_param_grad=False,
                             targets=None, n_mc_samples=1, top_n_classes=None):
    model.zero_grad()
    logits = model(inputs)
    n_examples, n_classes = logits.shape
    log_probs = F.log_softmax(logits, dim=1)
    probs = None

    def forward_and_backward(target, compute_param_grad=False):
        model.zero_grad()
        loss = F.nll_loss(log_probs, target)
        if compute_param_grad:
            loss.backward(retain_graph=True)
        else:
            with disable_param_grad(model):
                loss.backward(retain_graph=True)

    if FISHER_MC in fisher_types:
        probs = F.softmax(logits, dim=1)
        dist = torch.distributions.Categorical(probs)
        _targets = dist.sample((n_mc_samples,))
        for i in range(n_mc_samples):
            forward_and_backward(_targets[i])
            accumulate_op_results(model, FISHER_MC, scale=1/n_mc_samples/n_examples)

    if FISHER_EXACT in fisher_types:
        if probs is None:
            probs = F.softmax(logits, dim=1)
        probs, _targets = torch.sort(probs, dim=1, descending=True)
        sqrt_probs = torch.sqrt(probs)
        if top_n_classes is None:
            top_n_classes = n_classes
        for i in range(top_n_classes):
            set_op_grads_scale(model, sqrt_probs[:, i])
            forward_and_backward(_targets[:, i])
            accumulate_op_results(model, FISHER_EXACT, scale=1/n_examples)
        set_op_grads_scale(model, None)

    if FISHER_EMP in fisher_types:
        assert targets is not None
        forward_and_backward(targets, compute_emp_param_grad)
        move_op_results(model, FISHER_EMP, scale=1/n_examples)


def set_op_grads_scale(model, scale):
    for module in model.modules():
        operation = getattr(module, 'operation', None)
        if operation is None:
            continue
        operation.grads_scale = scale


def move_op_results(model, dst_attr, scale=1., accumulate=False):

    def scaling(src):
        if isinstance(src, dict):
            for s in zip(src.values()):
                scaling(s)
        else:
            assert isinstance(src, torch.Tensor)
            src.mul_(scale)

    def accumulation(src, dst):
        if isinstance(src, dict):
            for s, d in zip(src.values(), dst.values()):
                accumulation(s, d)
        else:
            assert isinstance(src, torch.Tensor)
            assert isinstance(dst, torch.Tensor)
            dst.add_(src)

    for module in model.modules():
        operation = getattr(module, 'operation', None)
        if operation is None:
            continue
        src_results = operation.get_op_results()
        if scale != 1:
            scaling(src_results)
        dst_results = getattr(module, dst_attr, None)
        if (dst_results is None) or (not accumulate):
            setattr(module, dst_attr, src_results)
        else:
            accumulation(src_results, dst_results)
        operation.delete_op_results()


def accumulate_op_results(model, dst_attr, scale=1.):
    move_op_results(model, dst_attr, scale, accumulate=True)

