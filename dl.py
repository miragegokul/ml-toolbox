from .basic import np, progress_bar, master_bar, print_time
from .plotting import plt
from .metrics import *

import os
import math
import torch
import torch.optim as optim
import time
import subprocess

from sklearn.metrics import accuracy_score


def save_model(model, path):
    torch.save(model.state_dict(), path)


def load_model(model, path):
    model.load_state_dict(torch.load(path))


def get_optimizer(model, lr=0.01, wd=5e-4, optimizer_type='adam'):
    """
    TODO: add support for this
         https://pytorch.org/docs/stable/optim.html#per-parameter-options
         https://discuss.pytorch.org/t/implementing-differential-learning-rate-by-parameter-groups/32903
    """
    parameters = filter(lambda p: p.requires_grad, model.parameters())
    optim = torch.optim.Adam(parameters, lr=lr, weight_decay=wd, )
    return optim


def lr_range_finder(model, train_dl, loss_criteria=None,
                    lr_low=1e-5, lr_high=10, beta=0.98, epochs=1):

    os.makedirs('models', exist_ok=True)
    p = "models/tmp.pth"
    save_model(model, str(p))

    lrs = np.logspace(math.log10(lr_low), math.log10(lr_high),
                      num=epochs * len(train_dl), endpoint=True)
    log_lrs = np.log10(lrs)

    avg_loss, best_loss, btch_idx = 0., 0., 0
    losses = []
    smooth_losses = []

    model.train()

    mb = master_bar(range(epochs))
    for ep in mb:
        for x, y in progress_bar(train_dl, parent=mb):
            optimizer = get_optimizer(model=model, lr=lrs[btch_idx])
            btch_idx += 1
            x = x.float().cuda()
            y = y.float().cuda().unsqueeze(1)
            optimizer.zero_grad()
            out = model(x)
            loss = loss_criteria(out, y)

            # Compute the smoothed loss
            avg_loss = beta * avg_loss + (1 - beta) * loss.item()
            smoothed_loss = avg_loss / (1 - (beta ** btch_idx))

            # Stop if the loss is exploding
            if btch_idx > 1 and smoothed_loss > 4 * best_loss:
                return log_lrs[:(btch_idx - 1)], losses, smooth_losses

            # Record the best loss
            if smoothed_loss < best_loss or btch_idx == 1:
                best_loss = smoothed_loss

            # Store the values
            losses.append(loss.item())
            smooth_losses.append(smoothed_loss)

            loss.backward()
            optimizer.step()

    load_model(model, p)
    return log_lrs, losses, smooth_losses


def plot_lr(log_lrs, losses, smooth_losses):
    plt.plot(log_lrs, losses, label='actual loss')
    plt.plot(log_lrs, smooth_losses, label='smoothed loss')
    plt.legend(loc='upper left')
    plt.xlabel(r'$log_{10}(LearningRate)$')
    plt.ylabel('Loss')
    plt.show()


def get_triangular_lr(lr_low=1e-5, lr_high=0.1,
                      iterations=None, half_cycle_pct=0.45):

    half_cycle = int(half_cycle_pct * iterations)
    left = iterations - 2 * half_cycle
    lrs = np.concatenate([np.linspace(lr_low, lr_high, num=half_cycle, endpoint=False),
                          np.linspace(lr_high, lr_low, num=half_cycle, endpoint=False),
                          np.linspace(lr_low, 0., num=left, endpoint=False)])

    mom = np.concatenate([np.linspace(0.95, 0.85, num=half_cycle, endpoint=False),
                          np.linspace(0.85, 0.95, num=half_cycle, endpoint=False),
                          [0.95 for i in range(left)]])
    assert mom.shape == lrs.shape
    return lrs, mom


def val_metrics(model, valid_dl, mb=None, metrics=[accuracy_score],
                loss_criteria=None):

    model.eval()
    total, sum_loss = 0., 0.
    y_true = []
    y_pred = []

    for x, y in progress_bar(valid_dl, parent=mb, leave=(mb is None)):
        batch = y.shape[0]
        x = x.float().cuda()
        y = y.float().cuda()
        out = model(x)
        loss = loss_criteria(out, y.float().unsqueeze(1))
        sum_loss += batch * (loss.item())
        total += batch
        y_pred.append(out.squeeze().detach().cpu() > 0)
        y_true.append(y.cpu().numpy())
    y_pred = np.concatenate(y_pred)
    y_true = np.concatenate(y_true)

    result = {}
    result['val_loss'] = sum_loss / total
    for m in metrics:
        result[m.__name__] = m(y_pred=y_pred, y_true=y_true)

    return result


@print_time
def train_triangular_policy(model, train_dl, valid_dl,
                            loss_criteria=None, lr_low=1e-5,
                            lr_high=0.01, epochs=4, metrics=[accuracy_score]):
    idx = 0
    iterations = epochs * len(train_dl)
    lrs, mom = get_triangular_lr(lr_low, lr_high, iterations)
    mb = master_bar(range(epochs))

    for ep in mb:
        model.train()
        total = 0
        sum_loss = 0
        for x, y in progress_bar(train_dl, parent=mb):
            optimizer = get_optimizer(model=model, lr=lrs[idx], wd=0)
            batch = y.shape[0]
            x = x.float().cuda()
            y = y.float().cuda()
            out = model(x)
            loss = loss_criteria(out, y.float().unsqueeze(1))
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            idx += 1
            total += batch
            sum_loss += batch * (loss.item())
        val_res = val_metrics(model=model, valid_dl=valid_dl, mb=mb,
                              loss_criteria=loss_criteria, metrics=metrics)
        train_loss = sum_loss / total
        print_metric = f"Epoch No.:{ep+1}, Train loss: {train_loss:.4f}, "
        print_metric += ''.join([f"{k}: {v:.4f}, " for k, v in val_res.items()])
        print(print_metric)
    return True


def training_loop(model, train_dl, valid_dl, steps=3,
                  loss_criteria=None, lr_low=1e-6,
                  lr_high=0.01, epochs=4, metrics=[accuracy_score]):
    for i in range(steps):
        loss = train_triangular_policy(model=model, train_dl=train_dl,
                                       valid_dl=valid_dl,
                                       loss_criteria=loss_criteria,
                                       lr_low=lr_low, lr_high=lr_high,
                                       epochs=epochs, metrics=metrics)


def set_trainable_attr(model, b=True):
    for p in model.parameters():
        p.requires_grad = b


def unfreeze(model, l, group_id='top_model'):
    top_model = getattr(model, group_id)
    set_trainable_attr(top_model[l])


def predict(model, data_dl, is_valid=False):
    if is_valid:
        y_true = []
    y_pred = []
    model.eval()
    for x, y in progress_bar(data_dl):
        x = x.float().cuda()
        out = model(x)
        y_pred.append(out.squeeze().detach().cpu() > 0)
        if is_valid:
            y_true.append(y.cpu().numpy())
    y_pred = np.concatenate(y_pred)
    if is_valid:
        y_true = np.concatenate(y_true)
        return y_pred, y_true
    return y_pred
