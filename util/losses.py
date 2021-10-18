import os
os.environ["TF_CPP_MIN_LOG_LEVEL"] = "2"

import torch
import numpy as np
import matplotlib.pyplot as plt


def compute_iou(x1, y1, w1, h1, x2, y2, w2, h2):
    # x1...:[b,GRIDSZ,GRIDSZ,5]
    xmin1 = x1 - 0.5 * w1
    xmax1 = x1 + 0.5 * w1
    ymin1 = y1 - 0.5 * h1
    ymax1 = y1 + 0.5 * h1

    xmin2 = x2 - 0.5 * w2
    xmax2 = x2 + 0.5 * w2
    ymin2 = y2 - 0.5 * h2
    ymax2 = y2 + 0.5 * h2

    # (xmin1,ymin1,xmax1,ymax1), (xmin2,ymin2,xmax2,ymax2)
    interw  =   torch.minimum(xmax1, xmax2) - torch.maximum(xmin1, xmin2)
    interh  =   torch.minimum(ymax1, ymax2) - torch.maximum(ymin1, ymin2)
    inter   =   torch.clip(interw, 0) * torch.clip(interh, 0)
    union   =   w1 * h1 + w2 * h2 - inter
    iou     =   inter / (union + 1e-6)
    return iou   # [b,GRIDSZ,GRIDSZ,5]


def cartesian_coordinate(batch_size, grid_size):
    """ return cartesian base torch.Tensor """
    x_grid, y_grid  =   torch.arange(grid_size), torch.arange(grid_size)
    # x_grid, y_grid shape: (GRIDSZ, GRIDSZ)
    y_grid, x_grid  =   torch.meshgrid(y_grid, x_grid)
    # should expand dim into (1, GRIDSZ, GRIDSZ, 1, 1)  represents: (batch_sz, M, N, n_anchors, xy)
    x_grid  = x_grid.view(1, grid_size, grid_size, 1, 1)
    y_grid  = y_grid.view(1, grid_size, grid_size, 1, 1)

    xy_grid = torch.cat([x_grid, y_grid], dim=-1)               # (1,GRIDSZ,GRIDSZ,1,2)
    xy_grid = torch.tile(xy_grid, (batch_size, 1, 1, 4, 1))     # (b,GRIDSZ,GRIDSZ,n_anchors,2)
    return xy_grid


class Prediction:
    """ Comprehend the output of the model in the forward propagation."""
    def __init__(self, outputs: torch.Tensor, grid_size, anchors, n_classes, device):
        self.data      =   outputs
        self.device    =   device
        self.n_class   =   n_classes
        self.grid_size =   grid_size
        self.anchors   =   torch.tensor(anchors, dtype=torch.float32, device=self.device)
        if len(self.anchors.shape) != 2:
            self.anchors = self.anchors.view(-1, 2)

        self.assertion()

    def assertion(self):
        # Dimension assertions: (Batch, Grid_size, Grid_size, N_anchors, 4+1+n_classes)
        # in last dimension: (x_bias, y_bias, w_bias, h_bias, conf, cls_0, ..., cls_n)
        assert self.data.ndim == 5
        assert self.data.shape[1:3] == (self.grid_size, self.grid_size)
        assert self.data.shape[3] == self.anchors.shape[0]
        assert self.data.shape[4] == 4 + 1 + self.n_class

    @property
    def xy(self):
        # (Batch, Grid_size, Grid_size, N_anchors, x-y)
        xy = self.data[..., :2]
        x_g, y_g = np.meshgrid(np.arange(xy.shape[1]), np.arange(xy.shape[2]))
        # (Grid_size, Grid_size) + (Grid_size, Grid_size) -> (1, Grid_size, Grid_size, 1, 2)
        xy_grid = torch.from_numpy(np.r_["4,5,1", x_g, y_g]).to(self.device)
        # (1, gdsz, gdsz, 1, 2) + (b, gdsz, gdsz, N_anchors, x-y) -> (b, gdsz, gdsz, N_anchors, x-y)
        return torch.sigmoid(xy) + xy_grid

    @ property
    def wh(self):
        # (Batch, Grid_size, Grid_size, N_anchors, w-h)
        return self.anchors * torch.exp(self.data[..., 2:4])

    @property
    def conf(self):
        # (Batch, Grid_size, Grid_size, N_anchors, conf)
        return torch.sigmoid(self.data[..., 4:5])

    @property
    def class_scores(self):
        # (Batch, Grid_size, Grid_size, N_anchors, N_classes)
        return self.data[..., 5:]


class YoloLoss:

    def __init__(self, device, anchors, grid_size, n_classes):
        self.device     =   device
        self.accuracy   =   0
        self.anchors    =   anchors
        self.grid_size  =   grid_size
        self.n_classes  =   n_classes

    @staticmethod
    def coordinate_loss(truth_xy, pred_xy, truth_wh, pred_wh, truth_mask, truth_nobj):
        """ [b,GRIDSZ,GRIDSZ,N_anchors,5+n_classes] x-y-w-h-conf-l0-l1-l2-l3
        Map the predicted bias to the truth value by the grid size
        All of the inputs is torch.Tensor """
        # [b,GRIDSZ,GRIDSZ,N_anchors,2] - [b,GRIDSZ,GRIDSZ,N_anchors,2]
        xy_loss = truth_mask*torch.square(truth_xy - pred_xy)
        xy_loss = torch.sum(xy_loss) / (truth_nobj + 1e-6)

        # [b,GRIDSZ,GRIDSZ,N_anchors,2] - [b,GRIDSZ,GRIDSZ,N_anchors,2]
        wh_loss = truth_mask*torch.square(torch.sqrt(truth_wh)-torch.sqrt(pred_wh))
        wh_loss = torch.sum(wh_loss) / (truth_nobj + 1e-6)

        return xy_loss + wh_loss

    @staticmethod
    def class_loss(truth_classes_oh, truth_mask, truth_nobj, pred_classes):
        # truth_classes_oh: [b, GRID_SIZE, GRID_SIZE, N_anchor, n_classes] => [b, GRID_SIZE, GRID_SIZE, N_anchor]
        true_box_class  =   torch.argmax(truth_classes_oh, -1)
        # the input of CrossEntropyLoss should be (N, C, d1, d2, ...) vs (N, d1, d2, ...)
        pred_classes    =   pred_classes.permute((0, 4, 1, 2, 3))

        loss = torch.nn.CrossEntropyLoss(reduction="none")
        # Compute loss: [b, Grid_size,Grid_size,N_anchors,n_classes] vs [b,GRID_SIZE,GRID_SIZE,N_anchor,n_classes]
        class_loss = loss(pred_classes, true_box_class)
        # [b,GRIDSZ,GRIDSZ,N_anchors] => [b,GRIDSZ,GRIDSZ,N_anchors,1] * [b,GRIDSZ,GRIDSZ,N_anchors,1]
        class_loss = torch.unsqueeze(class_loss, -1) * truth_mask
        class_loss = torch.sum(class_loss) / (truth_nobj + 1e-6)
        return class_loss

    @staticmethod
    def object_loss(gtruth_boxes, truth_mask, truth_nobj, pred_xy, pred_wh, pred_conf):
        # gtruth_boxes [b, GRID_SIZE, GRID_SIZE, N_anchor, x-y-w-h-l] -> [5, b, GRID_SIZE, GRID_SIZE, N_anchor]
        gt_boxes = gtruth_boxes.permute(4, 0, 1, 2, 3)
        x1, y1, w1, h1 =  gt_boxes[:4]

        # (Batch, Grid_size, Grid_size, N_anchors, x-y)
        x2, y2 = pred_xy[..., 0], pred_xy[..., 1]
        w2, h2 = pred_wh[..., 0], pred_wh[..., 1]

        # [b,GRIDSZ,GRIDSZ,4] -> [b,GRIDSZ,GRIDSZ,4,1]
        ious = compute_iou(x1, y1, w1, h1, x2, y2, w2, h2).unsqueeze(-1)
        accuracy = torch.sum(truth_mask*ious)/(truth_nobj + 1e-6)

        obj_loss = torch.sum(truth_mask*torch.square(ious-pred_conf))/(truth_nobj + 1e-6)
        return obj_loss, accuracy

    @staticmethod
    def non_object_loss(truth_boxes_grid, truth_mask, pred_xy, pred_wh, pred_conf):
        # Predictions
        # [b,GSZ,GSZ,N_anchor,2] => [b,GSZ,GSZ,N_anchor, 1, 2]
        pred_xy = torch.unsqueeze(pred_xy, dim=4)
        # [b,GSZ,GSZ,N_anchor,2] => [b,GSZ,GSZ,N_anchor, 1, 2]
        pred_wh = torch.unsqueeze(pred_wh, dim=4)
        pred_wh_half = pred_wh / 2.
        pred_xymin = pred_xy - pred_wh_half    # [b,GSZ,GSZ,N_anchor, 1, 2]
        pred_xymax = pred_xy + pred_wh_half    # [b,GSZ,GSZ,N_anchor, 1, 2]

        # Ground Truth
        # [b, n_labels, 5] => [b, 1, 1, 1, n_labels, 5]
        b, n_lbs, len_box = truth_boxes_grid.shape
        true_boxes_grid = truth_boxes_grid.view(b, 1, 1, 1, n_lbs, len_box)
        true_xy = true_boxes_grid[..., 0:2]   # [b, 1, 1, 1, n_labels, 2]
        true_wh = true_boxes_grid[..., 2:4]   # [b, 1, 1, 1, n_labels, 2]
        true_wh_half = true_wh / 2.
        true_xymin = true_xy - true_wh_half
        true_xymax = true_xy + true_wh_half

        # Compute non object loss from predxymin, predxymax, true_xymin, true_xymax
        # [b,GSZ,GSZ,N_anchor,1,2] vs [b,1,1,1,296,2] =>[b,GSZ,GSZ,N_anchor,296,2]
        intersectxymin = torch.maximum(pred_xymin, true_xymin)
        # [b,GSZ,GSZ,N_anchor,1,2] vs [b,1,1,1,296,2] =>[b,GSZ,GSZ,N_anchor,296,2]
        intersectxymax = torch.minimum(pred_xymax, true_xymax)
        # [b,GSZ,GSZ,N_anchor,296,2]
        intersect_wh = torch.maximum(intersectxymax - intersectxymin, torch.zeros_like(intersectxymax))
        # [b,GSZ,GSZ,N_anchor,296]*[b,GSZ,GSZ,N_anchor,296] =>[b,GSZ,GSZ,N_anchor,296]
        intersect_area = intersect_wh[..., 0] * intersect_wh[..., 1]
        # [b,GSZ,GSZ,N_anchor] * [b,GSZ,GSZ,N_anchor]
        pred_area = pred_wh[..., 0] * pred_wh[..., 1]
        # [b,1,1,1,296] * [b,1,1,1,296]
        true_area = true_wh[..., 0] * true_wh[..., 1]
        # [b,GSZ,GSZ,N_anchor,1]+[b,1,1,1,296] -[b,GSZ,GSZ,N_anchor,296]=>[b,GSZ,GSZ,N_anchor,296]
        union_area = pred_area + true_area - intersect_area
        # [b,GSZ,GSZ,N_anchor,296]
        iou_score = intersect_area / union_area
        # [b,GSZ,GSZ,N_anchor] => [b,GSZ,GSZ,N_anchor,1]
        best_iou = torch.amax(iou_score, dim=4).unsqueeze(-1)

        nonobj_detection = (best_iou < 0.6).float()
        nonobj_mask = nonobj_detection * (1 - truth_mask)

        # nonobj counter
        n_nonobj    =   torch.sum((nonobj_mask > 0.).to(torch.float32))
        nonobj_loss =   (torch.sum(nonobj_mask * torch.square(-pred_conf)) / (n_nonobj + 1e-6))
        return nonobj_loss

    def __call__(self, y_pred, y_truth, w=None, *args, **kwargs):
        """
        Loss: distance between ground truth and prediction\n
        Parameters
        ----------
        y_pred: torch.Tensor
            [b,GRIDSZ,GRIDSZ,N_anchor,9] x-y-w-h-conf-l0-l1-l2-l3

        y_truth: tuple
            mask: torch.Tensor
                [b,GRIDSZ,GRIDSZ,N_anchor,1]
            gt_boxes: torch.Tensor
                [b,GRIDSZ,GRIDSZ,N_anchor,5] x-y-w-h-l
            classes_oh: torch.Tensor
                [b,GRIDSZ,GRIDSZ,N_anchor,N_classes] l1-l2
            boxes_grid: torch.Tensor
                [b,N_labels,5] x-y-w-h-l
        """
        if w is None:
            w = {"obj": 5, "non_obj": 1, "coord": 1, "cls": 1}

        # Ground Truth
        #       detect_mask           [[b, GRID_SIZE, GRID_SIZE, N_anchor, 1],
        #       matching_gTruth_boxes  [b, GRID_SIZE, GRID_SIZE, N_anchor, x-y-w-h-l],
        #       class_onehot           [b, GRID_SIZE, GRID_SIZE, N_anchor, n_classes],
        #       gTruth_boxes_grid      [b, N_labels,  x-y-w-h-l]]
        truth_mask, gTruth_boxes, truth_classes_oh, truth_boxes_grid = y_truth
        truth_nObj  =   torch.sum(truth_mask)       # int
        truth_xy    =   gTruth_boxes[..., :2]       # [b, gsz, gsz, n_anc, 2]
        truth_wh    =   gTruth_boxes[..., 2:4]      # [b, gsz, gsz, n_anc, 2]

        # Predictions, y_pred shape: (b, grid_size, grid_size, n_anchors, 5+n_cls)
        pred = Prediction(y_pred, self.grid_size, self.anchors, self.n_classes, self.device)

        # Losses
        coord_loss  =   self.coordinate_loss(truth_xy=truth_xy, pred_xy=pred.xy, truth_wh=truth_wh,
                                             pred_wh=pred.wh, truth_mask=truth_mask, truth_nobj=truth_nObj)

        class_loss  =   self.class_loss(truth_classes_oh=truth_classes_oh, truth_mask=truth_mask,
                                        truth_nobj=truth_nObj, pred_classes=pred.class_scores)

        obj_loss, acc =   self.object_loss(gtruth_boxes=gTruth_boxes, truth_mask=truth_mask, truth_nobj=truth_nObj,
                                           pred_xy=pred.xy, pred_wh=pred.wh, pred_conf=pred.conf)

        nonobj_loss =   self.non_object_loss(truth_boxes_grid=truth_boxes_grid, truth_mask=truth_mask,
                                             pred_xy=pred.xy, pred_wh=pred.wh, pred_conf=pred.conf)

        self.accuracy = acc
        self.loss = w["coord"]*coord_loss + w["cls"]*class_loss + w["obj"]*obj_loss + w["non_obj"]*nonobj_loss
        return self.loss


def plan_loss_plot(plan_fpath, plt_row, plt_col):
    if not plt.get_backend() == "tkagg":
        plt.switch_backend("tkagg")
    fig, axs = plt.subplots(plt_row, plt_col, constrained_layout=True)
    mng = plt.get_current_fig_manager()
    mng.window.state("zoomed")
    loss_files = [file for file in os.listdir(plan_fpath)
                  if file.startswith("losses")]
    for i, loss_name in enumerate(loss_files):
        row = i // plt_col
        col = i - row*plt_col
        ax = axs[row, col]
        loss_fullname = os.path.join(plan_fpath, loss_name)

        params = {}
        train_loss = []
        valid_loss = []
        accuracy = []
        with open(loss_fullname, "r") as f:
            container_label = ""
            for line in f:
                if line.startswith(("Train", "Valid", "Acc")):
                    if line.startswith("Train"):
                        container_label = "train"
                    elif line.startswith("Valid"):
                        container_label = "valid"
                    else:
                        container_label = "acc"
                elif line.startswith(("Initial", "Max", "Decay")):
                    key, val = line[:-1].split(":")
                    params.update({key: val})
                else:
                    if container_label == "train":
                        train_loss.append(float(line[:-1]))
                    elif container_label == "valid":
                        valid_loss.append(float(line[:-1]))
                    elif container_label == "acc":
                        accuracy.append(float(line[:-1]))

        title = loss_name.removeprefix("losses_").removesuffix(".txt")
        ax.set_title(title, fontsize=10, fontweight="bold")
        ax.plot(train_loss, label="Train")
        ax.plot(valid_loss, label="valid")
        ax.scatter(len(valid_loss)-1, valid_loss[-1], c="black")
        ax.annotate("Model loss\n     %.3f" % valid_loss[-1],
                    xy=(len(valid_loss)-1, valid_loss[-1]),
                    xycoords="data", textcoords="offset points",
                    xytext=(-30, 5), fontsize=8)
        ax.tick_params(axis="both", labelsize=8)
        for n, key in enumerate(params.keys()):
            ax.annotate("%s: %s" % (key, params[key]),
                        xy=[.2*len(accuracy), .4*train_loss[0]],
                        xycoords="data", textcoords="offset points",
                        xytext=(10, n*10), fontsize=8)
        ax_c = ax.twinx()
        ax_c.tick_params(axis="y", labelcolor="green", labelsize=8)
        ax_c.plot(accuracy, label="Acc", c="green")
        ax_c.scatter(len(accuracy)-1, accuracy[-1], c="red")
        ax_c.annotate("Model Accuracy\n     %.3f" % accuracy[-1],
                      xy=(len(accuracy)-1, accuracy[-1]),
                      xycoords="data", textcoords="offset points",
                      xytext=(-50, -20), fontsize=8)
    plt.show()
