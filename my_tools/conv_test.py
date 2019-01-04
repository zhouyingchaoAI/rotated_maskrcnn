import numpy as np
import cv2
import sys

import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim


class _NewEmptyTensorOp(torch.autograd.Function):
    @staticmethod
    def forward(ctx, x, new_shape):
        ctx.shape = x.shape
        return x.new_empty(new_shape)

    @staticmethod
    def backward(ctx, grad):
        shape = ctx.shape
        return _NewEmptyTensorOp.apply(grad, shape), None

class Conv2d(torch.nn.Conv2d):
    def forward(self, x):
        if x.numel() > 0:
            return super(Conv2d, self).forward(x)
        # get output shape

        output_shape = [
            (i + 2 * p - (di * (k - 1) + 1)) // d + 1
            for i, p, di, k, d in zip(
                x.shape[-2:], self.padding, self.dilation, self.kernel_size, self.stride
            )
        ]
        output_shape = [x.shape[0], self.weight.shape[0]] + output_shape
        return _NewEmptyTensorOp.apply(x, output_shape)

class ConvTranspose2d(torch.nn.ConvTranspose2d):
    def forward(self, x):
        if x.numel() > 0:
            return super(ConvTranspose2d, self).forward(x)
        # get output shape

        output_shape = [
            (i - 1) * d - 2 * p + (di * (k - 1) + 1) + op
            for i, p, di, k, d, op in zip(
                x.shape[-2:],
                self.padding,
                self.dilation,
                self.kernel_size,
                self.stride,
                self.output_padding,
            )
        ]
        output_shape = [x.shape[0], self.bias.shape[0]] + output_shape
        return _NewEmptyTensorOp.apply(x, output_shape)

class persistent_locals(object):
    def __init__(self, func):
        self._locals = {}
        self.func = func

    def __call__(self, *args, **kwargs):
        def tracer(frame, event, arg):
            if event=='return':
                l = frame.f_locals.copy()
                self._locals = l
                for k,v in l.items():
                    globals()[k] = v

        # tracer is activated on next call, return or exception
        sys.setprofile(tracer)
        try:
            # trace the function call
            res = self.func(*args, **kwargs)
            
        finally:
            # disable tracer and replace with old one
            sys.setprofile(None)
        return res

    def clear_locals(self):
        self._locals = {}

    @property
    def locals(self):
        return self._locals

def conv_transpose2d_by_factor(in_cn, out_cn, factor):
    """
    Maintain output_size = input_size * factor (multiple of 2)
    """
    # stride = int(1.0/spatial_scale)
    assert factor >= 2 and factor % 2 == 0
    stride = factor
    k = stride * 2
    kernel_size = (k,k)
    p = stride // 2
    padding = (p, p)
    stride = (stride, stride)
    return ConvTranspose2d(in_cn, out_cn, kernel_size, stride, padding)


class DataGenerator():
    def __init__(self):
        IMG_SIZE = 56
        self.H = IMG_SIZE
        self.W = IMG_SIZE

    def next_batch(self, batch_size=8):
        data = [self._get_random_data() for i in range(batch_size)]
        return data

    def _get_random_data(self, depth=None):

        sz = (self.H, self.W)
        m = np.zeros((self.H, self.W, 3), dtype=np.float32)
        m_gt = np.zeros(sz, dtype=np.float32)

        if depth is None:
            depth = float(np.random.randint(-50,50)) / 10

        m_gt[:] = depth 

        # set first channel to depth values + gaussian noise
        mean = 0
        std = 0.03
        m[:,:,0] = depth + np.random.normal(mean, std, size=sz)
        # set second and third channels as random noise
        m[:,:,1] = np.random.random(size=sz)
        m[:,:,2] = np.random.random(size=sz)

        return [m, m_gt, depth]

    def convert_data_batch_to_tensor(self, data, use_cuda=False):
        m_data = []
        m_gt_data = []

        for d in data:
            m = d[0]
            m = np.transpose(m, [2,0,1])
            m_data.append(m)
            m_gt_data.append(d[1])

        tm = torch.FloatTensor(m_data)#.unsqueeze(0)
        tmgt = torch.FloatTensor(m_gt_data).unsqueeze(1)

        if use_cuda:
            tm = tm.cuda()
            tmgt = tmgt.cuda()
        return tm, tmgt


class ConvNet(nn.Module):
    def __init__(self, in_channels=3):
        super(ConvNet, self).__init__()

        conv1_filters = 32
        conv2_filters = 64
        conv3_filters = 64

        self.conv1 = nn.Conv2d(in_channels, conv1_filters, kernel_size=3, stride=2, padding=1)
        self.conv2 = nn.Conv2d(conv1_filters, conv2_filters, kernel_size=3, stride=2, padding=1)
        self.conv3 = nn.Conv2d(conv2_filters, conv3_filters, kernel_size=3, stride=2, padding=1)
        self.bn1 = nn.BatchNorm2d(conv1_filters)
        self.bn2 = nn.BatchNorm2d(conv2_filters)
        self.bn3 = nn.BatchNorm2d(conv3_filters)

        conv_t_filters = 64
        self.conv_t1 = conv_transpose2d_by_factor(conv3_filters, conv_t_filters, factor=2)
        self.conv_t2 = conv_transpose2d_by_factor(conv_t_filters, conv_t_filters, factor=2)
        self.conv_t3 = conv_transpose2d_by_factor(conv_t_filters, 1, factor=2)
        # self.depth_reg = Conv2d(conv_t_filters, 1, 5, 1, 5 // 2)

    def forward(self, x):
        batch_sz = len(x)
        c1 = F.relu(self.bn1(self.conv1(x)))
        c2 = F.relu(self.bn2(self.conv2(c1)))
        c3 = F.relu(self.bn3(self.conv3(c2)))
        ct1 = F.relu(self.conv_t1(c3))
        ct2 = F.relu(self.conv_t2(ct1))
        ct3 = self.conv_t3(ct2)
        return ct3 #self.depth_reg(F.relu(ct3))


def l1_loss(x, y):
    return torch.abs(x - y)

def mean_var_loss(input, target):
    
    diff = target - input
    md = torch.mean(diff)
    var_loss = (1 + torch.abs(diff - md)) ** 2 - 1
    # var_loss = (input - torch.mean(input))

    loss = 0.5 * torch.abs(diff) + 0.5 * var_loss

    # mean_p = torch.mean(input)
    # mean_t = torch.mean(target)
    # mean_loss = torch.abs(mean_t - mean_p)
    # loss = 0.5 * mean_loss + 1.5 * var_loss

    return loss

# @persistent_locals
def train(model, dg):

    epochs = 10
    n_iters = 1000
    batch_size = 32
    lr = 1e-3

    model.train()

    optimizer = optim.Adam(model.parameters(), lr=lr, betas=(0.9, 0.999))

    losses = []
    for iter in range(n_iters):
        data = dg.next_batch(batch_size)

        train_x, train_y = dg.convert_data_batch_to_tensor(data, use_cuda=True)
        optimizer.zero_grad()

        output = model(train_x)

        # loss
        # loss_type = "l1"
        # loss = l1_loss(output, train_y).mean()
        loss_type = "mean_var"
        loss = mean_var_loss(output, train_y).mean()
        loss.backward()
        optimizer.step()

        losses.append(loss.item())

        if iter % 20 == 0 and iter > 0:
            print("iter %d of %d -> Total loss (%s): %.3f"%(iter, n_iters, loss_type, np.mean(losses)))
            losses = []

    print("iter %d of %d -> Total loss: %.3f"%(iter, n_iters, loss.item()))

# @persistent_locals
def test(model, dg, batch_sz = 8, verbose=False):
    model.eval()
    # batch_sz = 100
    # test_data = dg.next_batch(test_batch_sz)
    test_data = [dg._get_random_data(float(np.random.randint(-10,10)) / 14) for i in range(batch_sz)]
    test_x, test_y = dg.convert_data_batch_to_tensor(test_data, use_cuda=True)

    preds = model(test_x)
    preds = preds.detach().cpu().numpy()

    mean_arr = []
    std_arr = []
    for ix,p in enumerate(preds):
        gt = test_data[ix][-1]
        err = np.mean(np.abs(p - gt))
        # x = test_data[ix][0][:,:,0]
        # x_mean = np.mean(x)
        # x_std = np.std(x)
        p_mean = np.mean(p)
        p_std = np.std(p)
        err_diff = gt - p_mean
        mean_arr.append(np.abs(err_diff))
        std_arr.append(p_std)
        if verbose:
            print("Abs error: %.3f, GT: %.3f, Pred Mean: %.3f (err diff: %.3f), Pred Std: %.3f"%(err, gt, p_mean, err_diff, p_std))
    print("Batch size: %d -> Average mean err: %.3f, Average std: %.3f"%(batch_sz, np.mean(mean_arr), np.mean(std_arr)))

if __name__ == '__main__':
    dg = DataGenerator()
    batch_sz = 8
    data = dg.next_batch(batch_sz)
    x = dg.convert_data_batch_to_tensor(data)

    model = ConvNet(in_channels=3)
    model.cuda()
    print("Model constructed")

    train(model, dg)
    test(model, dg, 200, False)
    # test(model, dg, 10, verbose=True)