import os
os.environ['KMP_DUPLICATE_LIB_OK'] = 'TRUE'
from pprint                 import pformat
from os.path                import join
from xml.etree              import ElementTree as ET
from xml.dom.minidom        import parseString

import torch
import numpy                as np
import matplotlib           as mpl
import matplotlib.pyplot    as plt
from matplotlib             import figure
from skimage.io             import imread, imsave
from skimage.util           import dtype_limits, img_as_ubyte
from torch.utils.data       import Dataset

from cccode.image           import Check

IMGSZ       =   320
GRIDSZ      =   40
IMG_PLUGIN  =   "simpleitk"
F32         =   torch.float32
ck          =   Check(False, False, False)
ANCHORS     =   [1., 1., 1.125, 1.125, 1.25, 1.25, 1.375, 1.375]
FFOV_XML    =   "D:\\Workspace\\RBC Recognition\\data\\2021-01-05\\fov_annotations.xml"


class MultimodalSample:
    def __init__(self):
        self.sample_idx = None
        self.phase      = None
        self.amplitude  = None
        self.overfocus  = None
        self.underfocus = None
        self.labels     = []
        self.source_msg = {}

    def __repr__(self):
        output      =   ""
        output      +=  f"<Ojb MultimodalSample>\n"
        if self.sample_idx is not None:
            output  +=  f"\timage_index:    {self.sample_idx:<4d}\n"
        h, w        =   self.phase.shape
        output      +=  f"\tphase:          {type(self.phase)}, ({h}, {w}), " \
                        f"vrange: {dtype_limits(self.phase)}\n"
        h, w        =   self.amplitude.shape
        output      +=  f"\tamplitude:      {type(self.amplitude)}, ({h}, {w}), " \
                        f"vrange: {dtype_limits(self.amplitude)}\n"
        h, w        =   self.overfocus.shape
        output      +=  f"\tover-focus:     {type(self.overfocus)}, ({h}, {w}), " \
                        f"vrange: {dtype_limits(self.overfocus)}\n"
        h, w        =   self.underfocus.shape
        output      +=  f"\tunder-focus:    {type(self.underfocus)}, ({h}, {w}), " \
                        f"vrange: {dtype_limits(self.underfocus)}\n"
        output      +=  f"\tlabel numbers:  {len(self.labels)}\n"

        output      +=  f"\tsource_messages:\n"
        output      +=  pformat(self.source_msg, indent=8)
        return output

    @property
    def modalities(self):
        return self.amplitude, self.phase, self.underfocus, self.overfocus

    def set_modalities(self, amp, pha, under, over):
        self.amplitude  = amp
        self.phase      = pha
        self.overfocus  = over
        self.underfocus = under

    @classmethod
    def from_element(cls, sample: ET.Element):
        mm_sample       =   cls()
        amp_fullname    =   sample.find("amp_fullname").text
        pha_fullname    =   sample.find("pha_fullname").text
        over_fullname   =   sample.find("over_fullname").text
        under_fullname  =   sample.find("under_fullname").text

        mm_sample.source_msg.update({
            "amp_fullname": amp_fullname,
            "pha_fullname": pha_fullname,
            "over_fullname": over_fullname,
            "under_fullname": under_fullname
        })

        mm_sample.amplitude  =   imread(amp_fullname,    True, "simpleitk")
        mm_sample.phase      =   imread(pha_fullname,    True, "simpleitk")
        mm_sample.overfocus  =   imread(over_fullname,   True, "simpleitk")
        mm_sample.underfocus =   imread(under_fullname,  True, "simpleitk")

        mm_sample.sample_idx =   int(sample.find("image_idx").text)
        img_shape_elm   =   sample.find("image_shape")
        height, width   =   [int(img_shape_elm.find(tag).text) for tag in ["height", "width"]]
        assert mm_sample.phase.shape == (height, width)

        labels_elm      =   sample.find("labels")
        for lbl_elm in labels_elm.findall("label"):
            bbox_elm    =   lbl_elm.find("bbox")
            x, y, w, h  =   [int(bbox_elm.find(tag).text) for tag in ("x", "y", "w", "h")]
            lbl_cls     =   int(lbl_elm.find("class").text)
            mm_sample.labels.append((lbl_cls, x, y, w, h))
        return mm_sample

    @staticmethod
    def split(sample, n_split=3, target_size=(320, 320)):
        assert (sample.phase.shape == sample.amplitude.shape ==
                sample.underfocus.shape == sample.overfocus.shape)

        height,     width           =   sample.phase.shape
        tgt_height, tgt_width       =   target_size
        centroid_arrange_width      =   width-tgt_width
        centroid_arrange_height     =   height-tgt_height

        centroid_interval_width     =   centroid_arrange_width  // (n_split-1)
        centroid_interval_height    =   centroid_arrange_height // (n_split-1)

        subview_multimodal_samples  =   []
        for i in range(n_split):        # ROW split
            for k in range(n_split):    # COLUMN split
                x_centroid = tgt_width//2  + k*centroid_interval_width
                y_centroid = tgt_height//2 + i*centroid_interval_height

                x0 = x_centroid - tgt_width//2
                x1 = x_centroid + tgt_width//2
                y0 = y_centroid - tgt_height//2
                y1 = y_centroid + tgt_height//2

                subview_modalities = [image[y0:y1, x0:x1] for image in sample.modalities]

                subview_labels  = []
                for cls, x, y, w, h in sample.labels:
                    if x0 <= x < x1 and y0 <= y < y1:
                        subview_labels.append((cls, x-x0, y-y0, w, h))

                subview_mltSample = MultimodalSample()
                subview_mltSample.labels  = subview_labels
                subview_mltSample.source_msg.update({"parent_msg": sample.source_msg})
                subview_mltSample.set_modalities(*subview_modalities)
                subview_multimodal_samples.append(subview_mltSample)
        return subview_multimodal_samples

    def save(self, save_root):
        amp_path, pha_path, minus_path, plus_path, anno_path = [join(save_root, pth) for pth in
                                                                ("amp", "pha", "minus", "plus", "anno")]
        for pth in (amp_path, pha_path, minus_path, plus_path, anno_path):
            if not os.path.exists(pth):
                os.mkdir(pth)

        amp_fullname        =   join(amp_path,      f"amp_{self.sample_idx:04d}.jpg")
        pha_fullname        =   join(pha_path,      f"pha_{self.sample_idx:04d}.jpg")
        minus_fullname      =   join(minus_path,    f"minus_{self.sample_idx:04d}.jpg")
        plus_fullname       =   join(plus_path,     f"plus_{self.sample_idx:04d}.jpg")
        anno_fullname       =   join(anno_path,     f"pha_{self.sample_idx:04d}.txt")

        # save images
        for modality, fname in zip(self.modalities, (amp_fullname, pha_fullname, minus_fullname, plus_fullname)):
            # amplitude, phase, underfocus, overfocus
            imsave(fname, img_as_ubyte(modality), "simpleitk")

        # save labels
        yolo_string     =   ""
        height, width   =   self.amplitude.shape
        for cell_class, x, y, w, h in self.labels:
            # Coordinate transform from absolute coordination to yolo
            x1, y1 = x / width, y / height
            w1, h1 = w / width, h / height
            coord_str = " ".join([f"{coord:1.6f}" for coord in (x1, y1, w1, h1)])
            yolo_string += str(cell_class) + " " + coord_str + "\n"

        # save single sample yolo file
        with open(anno_fullname, "w") as f:
            f.write(yolo_string)

    def label_reshape(self):
        labels_array = []
        for cls, bbox in self.labels:
            labels_array.append([*bbox, cls])
        return np.array(labels_array)

    labels_array = property(label_reshape)

    def annotate_axes(self, ax: mpl.figure.Axes):
        for _, x, y, w, h in self.labels:
            rect = plt.Rectangle((x-w//2-1, y-h//2-1), w, h, fill=False, color="blue")
            ax.add_patch(rect)
        return ax


class StandardXMLContainer:
    """
    For each child sample, there are six SubElements:
        'image_shape', "image_idx', 'amp_fullname', 'pha_fullname', 'under_fullname',
        'over_fullname', and 'labels'
    The 'labels' stored all automatic annotated labels inside this sample,
    as the child element of 'labels'.
    """

    def __init__(self):
        self.root       =   ET.Element("FFoV_Annotation")
        self.samples    =   None

    @classmethod
    def fromXML(cls, filename):
        container           =   cls()
        etree               =   ET.parse(filename)
        container.root      =   etree.getroot()
        container.samples   =   [MultimodalSample.from_element(elm) for elm in container.root.findall("sample")]
        return container

    def sample_visual(self, start_idx=0, figsize=(20, 12), title=None, tt_fontsize=16):
        visualized_samples = self.samples[start_idx:start_idx+15]
        fig, axs = plt.subplots(3, 5, figsize=figsize, constrained_layout=True)
        if title is not None:
            plt.suptitle(title, fontproperties={"size": tt_fontsize})
        for row in range(3):
            for collum in range(5):
                n_sp    =   row*5 + collum
                ax      =   axs[row, collum]
                sample: MultimodalSample = visualized_samples[n_sp]
                ax.imshow(sample.phase, cmap="gray")
                ax.set_xticks([])
                ax.set_yticks([])
                for sp in ("top", "bottom", "left", "right"):
                    ax.spines[sp].set_visible(False)
                sample.annotate_axes(ax)
        plt.show()

    @staticmethod
    def subElem(parent: ET.Element, child_tag: str, text: str = None):
        child = ET.SubElement(parent, child_tag)
        if text is not None:
            child.text = text if isinstance(text, str) else str(text)
        return child

    @staticmethod
    def sample_msg(sample: ET.Element):
        output          =   "Sample"
        img_idx_str     =   sample.find('image_idx').text
        output          +=  f"\timage idx:      {img_idx_str}\n"

        shape_elm       =   sample.find("image_shape")
        height, width   =   [int(shape_elm.find(tag).text) for tag in ("height", "width")]
        output          +=  f"\timage shape:    ({height}, {height})\n"

        amp_fpath, amp_fname        =   os.path.split(sample.find("amp_fullname").text)
        pha_fpath, pha_fname        =   os.path.split(sample.find("pha_fullname").text)
        over_fpath, over_fname      =   os.path.split(sample.find("over_fullname").text)
        under_fpath, under_fname    =   os.path.split(sample.find("under_fullname").text)
        amp_root, amp_dir           =   os.path.split(amp_fpath)
        pha_root, pha_dir           =   os.path.split(pha_fpath)
        over_root, over_dir         =   os.path.split(over_fpath)
        under_root, under_dir       =   os.path.split(under_fpath)
        try:
            assert amp_root == pha_root == over_root == under_root
        except AssertionError as e:
            print("Path root different:", e)
            exit(1)

        output          +=  f"\tmodalities root: {amp_root}\n"
        output          +=  f"\t\tamplitude     dir: {amp_dir:<6s} filename: {amp_fname}\n"
        output          +=  f"\t\tphase         dir: {pha_dir:<6s} filename: {pha_fname}\n"
        output          +=  f"\t\tunder-focus   dir: {over_dir:<6s} filename: {under_fname}\n"
        output          +=  f"\t\tover-focus    dir: {under_dir:<6s} filename: {over_fname}\n"

        labels_elm      =   sample.find("labels")
        num_labels      =   len(labels_elm.findall("label"))
        output          +=  f"\tlabels number:  {num_labels}\n"
        return output

    def add_sample(self, idx, image_shape, amplitude_filename, phase_filename, minus_filename, plus_filename):
        sample  =   ET.SubElement(self.root, "sample")

        shape   =   self.subElem(sample, "image_shape")
        self.subElem(shape,  "height",          image_shape[0])
        self.subElem(shape,  "width",           image_shape[1])

        self.subElem(sample, "image_idx",       str(idx))
        self.subElem(sample, "amp_fullname",    amplitude_filename)
        self.subElem(sample, "pha_fullname",    phase_filename)
        self.subElem(sample, "over_fullname",   plus_filename)
        self.subElem(sample, "under_fullname",  minus_filename)
        self.subElem(sample, "labels")
        return sample

    def add_label(self, sample: ET.Element, x, y, w, h, cell_class, creator: str):
        node  = sample.find("labels")
        label = ET.SubElement(node,  "label")
        bbox  = ET.SubElement(label, "bbox")
        self.subElem(label, "class",    str(cell_class))
        self.subElem(label, "creator",  creator)

        # add all bounding box values
        assert isinstance(x, int) and isinstance(y, int) and isinstance(w, int) and isinstance(h, int)
        self.subElem(bbox, "x", int(x))
        self.subElem(bbox, "y", int(y))
        self.subElem(bbox, "w", int(w))
        self.subElem(bbox, "h", int(h))

    def compile(self, fpath: str = None):
        rough_string    =   ET.tostring(self.root, encoding="utf-8")
        reparsed        =   parseString(rough_string)
        pretty_xml_str  =   reparsed.toprettyxml(indent="\t")

        with open(fpath, "w") as f:
            f.write(pretty_xml_str)

    def toyolo(self, yolopath, prefix="phase"):
        if prefix == "phase":
            yolo_pattern    =   "phase_{:02d}.txt"
        else:
            yolo_pattern    =   "img_{:02d}.txt"

        for sample in self.root.findall("sample"):
            img_idx         =   int(sample.find("image_idx").text)
            image_shape     =   sample.find("image_shape")
            height, width   =   [int(image_shape.find(tag).text) for tag in ("height", "width")]
            yolo_string     =   ""

            for lbl in sample.find("labels").findall("label"):
                bbox        =   lbl.find("bbox")
                x, y, w, h  =   [int(bbox.find(tag).text) for tag in ("x", "y", "w", "h")]
                cell_class  =   lbl.find("class").text

                # Coordinate transform from absolute coordination to yolo
                x1, y1      =   x / width, y / height
                w1, h1      =   w / width, h / height
                coord_str   =   " ".join([f"{coord:1.6f}" for coord in (x1, y1, w1, h1)])
                yolo_string +=  cell_class + " " + coord_str + "\n"

            # save single sample yolo file
            dst_fullname    =   join(yolopath, yolo_pattern.format(img_idx))
            with open(dst_fullname, "w") as f:
                f.write(yolo_string)

    def sample_splitting(self, split_root="", samples_per_batch=None, target_size=(340, 340)):
        sample_set          =   []
        n_samples           =   len(self.samples)

        if split_root != "" and samples_per_batch is not None:
            n_batch         =   n_samples // samples_per_batch + 1
            batch_path      =   [join(split_root, f"batch_{n_bth:02d}") for n_bth in range(n_batch)]
            if not os.path.exists(split_root):
                os.mkdir(split_root)
            for pth in batch_path:
                if not os.path.exists(pth):
                    os.mkdir(pth)
        else:
            batch_path      =   None

        for i, sp in enumerate(self.samples):
            child_samples   =   MultimodalSample.split(sp, target_size=target_size)
            for k, child_sp in enumerate(child_samples):
                child_sp.sample_idx = i*9 + k

                if split_root != "":
                    if samples_per_batch is not None:
                        save_root   =   batch_path[i//samples_per_batch]
                    else:
                        save_root   =   split_root
                    child_sp.save(save_root)
            sample_set = np.hstack((sample_set, child_samples))
        return sample_set


class DataTransform:
    @staticmethod
    def process_true_boxes(gt_boxes, anchors, image_size):
        # gt_boxes: [296, 5]
        # 320 // 40 = 8
        scale           =   image_size // GRIDSZ
        # [4, 2]
        anchors         =   np.array(anchors).reshape((4, 2))

        # mask for object, for each grid, four boxes
        # one mask (box exist) value for each box
        detector_mask   =   np.zeros([GRIDSZ, GRIDSZ, 4, 1])
        # for each grid, four boxes
        # five value for each box: x-y-w-h-l
        matching_gt_box =   np.zeros([GRIDSZ, GRIDSZ, 4, 5])
        # [40,5] x1-y1-x2-y2-l => x-y-w-h-l
        gt_boxes_grid   =   np.zeros(gt_boxes.shape)

        for i, box in enumerate(gt_boxes):  # [286,5]
            # DB: tensor => numpy
            # box: [5], x1-y1-x2-y2-l
            # 320 => 32
            x = box[0] / scale
            y = box[1] / scale
            w = box[2] / scale
            h = box[3] / scale
            # [286,5] x-y-w-h-l
            gt_boxes_grid[i] = np.array([x, y, w, h, box[4]])

            if w * h > 0:  # valid box with object in it
                # Searching for best anchor according to IoU
                best_anchor = 0
                best_iou = 0
                for j in range(4):
                    interct =   np.minimum(w, anchors[j, 0])*np.minimum(h, anchors[j, 1])
                    union   =   w * h + (anchors[j, 0] * anchors[j, 1]) - interct
                    iou     =   interct / union

                    if iou > best_iou:  # best iou
                        best_anchor = j
                        best_iou    = iou
                        # found the best anchors
                if best_iou > 0:
                    x_coord = np.floor(x).astype(np.int32)
                    y_coord = np.floor(y).astype(np.int32)
                    # [b,h,w,4,1]
                    detector_mask[y_coord, x_coord, best_anchor] = 1
                    # [b,h,w,4,x-y-w-h-l]
                    matching_gt_box[y_coord, x_coord, best_anchor] = np.array([x, y, w, h, box[4]])

        # [296,5] => [32,32,4,5]
        # [32,32,4,5]
        # [32,32,4,1]
        # [296,5]
        return matching_gt_box, detector_mask, gt_boxes_grid

    @staticmethod
    def image_transform(image):
        image   =   torch.tensor(image)
        mu      =   torch.mean(image, dim=(1, 2), keepdim=True)
        sigma   =   torch.std(image, dim=(1, 2), keepdim=True)
        image   =   torch.sigmoid((image-mu)/sigma)
        return image

    @classmethod
    def target_transform(cls, label) -> tuple:
        """
        Returns
        -------
        mask, gt_box, class_oh, box_grid
        """
        gt_box, mask, grid = map(lambda x: torch.from_numpy(x).to(F32),
                                 cls.process_true_boxes(label, ANCHORS, IMGSZ))
        oh_base = torch.tile(torch.zeros_like(mask), (1, 1, 1, 5))
        class_oh = oh_base.scatter_(-1, gt_box[..., 4:].to(torch.int64), 1)
        return mask, gt_box, class_oh[..., 1:], grid


class BloodSmearDataset(Dataset):
    def __init__(self, xml_filename, image_transform=None, target_transform=None):
        self.max_boxes                  =   0
        self.image_transform            =   image_transform
        self.target_transform           =   target_transform

        sample_container                =   StandardXMLContainer.fromXML(xml_filename)
        sample_sets                     =   sample_container.sample_splitting()
        self.modalities, self.labels    =   self.load_datasets(sample_sets)

    def load_datasets(self, sample_sets: np.ndarray):
        # samplesets: np.array([MltSample1, MltSample2, MltSample3, ....])
        labels     = []
        modalities = []
        for i, subview_sample in enumerate(sample_sets):
            modalities.append(np.array(subview_sample.modalities))

            labels_array = subview_sample.labels_array
            if len(labels_array) > self.max_boxes:
                self.max_boxes = len(labels_array)
            labels.append(labels_array)

        boxes = np.zeros((len(labels), self.max_boxes, 5))
        for i, label in enumerate(labels):
            # overwrite the N boxes info  [N,5]
            boxes[i, :label.shape[0]] = label

        return modalities, boxes

    def __len__(self):
        return len(self.labels)

    def __getitem__(self, idx):
        label       =   self.labels[idx]
        modality    =   self.modalities[idx]
        # img numpy(320,320) label list(295,5) -> img (320,320) label (295,5)
        if self.image_transform:
            modality = self.image_transform(modality)
        if self.target_transform:
            label   = self.target_transform(label)
        sample = {"modality": modality, "label": label}
        return sample


def dataset_xml_from_annotations(minus_path, plus_path, focus_path, phase_path, annotations_path,
                                 xml_filename, image_format=".bmp", fixed_class="rbc", creator="auto"):
    """
    Now that the modality and tag data have been generated, it is necessary to combine
    these data into a single xml file to organize the subsequent training dataset.
    """
    minus_filenames     =   [fname for fname in os.listdir(minus_path)  if fname.endswith(image_format)]
    plus_filenames      =   [fname for fname in os.listdir(plus_path)   if fname.endswith(image_format)]
    focus_filenames     =   [fname for fname in os.listdir(focus_path)  if fname.endswith(image_format)]
    phase_filenames     =   [fname for fname in os.listdir(phase_path)  if fname.endswith(image_format)]
    ann_filenames       =   [fname for fname in os.listdir(annotations_path)  if fname.endswith(".txt")]

    # create root xml Element
    xml_container       =   StandardXMLContainer()

    for f_minus, f_plus, f_focus, f_phase, ann in zip(minus_filenames, plus_filenames, focus_filenames,
                                                      phase_filenames, ann_filenames):

        minus_idx   =   f_minus.removesuffix(image_format).removeprefix("minus_")
        plus_idx    =   f_plus.removesuffix(image_format).removeprefix("plus_")
        focus_idx   =   f_focus.removesuffix(image_format).removeprefix("focus_")
        phase_idx   =   f_phase.removesuffix(image_format).removeprefix("phase_")
        ann_idx     =   ann.removesuffix(".txt").removeprefix("auto_")

        try:
            assert minus_idx == plus_idx == focus_idx == phase_idx == ann_idx
        except AssertionError as e:
            print("File did not of same sample: ", e)
            raise AssertionError

        minus_fullname  =   join(minus_path,    f_minus)
        plus_fullname   =   join(plus_path,     f_plus)
        focus_fullname  =   join(focus_path,    f_focus)
        phase_fullname  =   join(phase_path,    f_phase)
        ann_fullname    =   join(annotations_path, ann)

        # add sample
        img_idx         =   int(minus_idx)
        image_shape     =   imread(phase_fullname, True, "simpleitk").shape             # load image shape
        sample          =   xml_container.add_sample(img_idx,        image_shape,
                                                     focus_fullname, phase_fullname,
                                                     minus_fullname, plus_fullname)

        # Loading the annotations
        with open(ann_fullname, "r") as f:
            for line in f:
                if line.endswith("\n"):
                    line = line.removesuffix("\n")
                num_strings     =   list(filter(lambda elm: elm != "", line.split(" ")))
                x, y, w, h      =   [int(num) for num in num_strings]

                # Add label
                if fixed_class == "rbc":
                    clsn = 0
                else:
                    raise Exception("wrong label class")
                xml_container.add_label(sample, x, y, w, h, clsn, creator)

    # writes into disk
    xml_container.compile(xml_filename)
    return xml_container


TRAIN_DS_CONSTRUCTOR = {"xml_filename":     FFOV_XML,
                        "set_name":         "training",
                        "image_transform":  DataTransform.image_transform,
                        "target_transform": DataTransform.target_transform}

VALID_DS_CONSTRUCTOR = {"xml_filename":     FFOV_XML,
                        "set_name":         "validating",
                        "image_transform":  DataTransform.image_transform,
                        "target_transform": DataTransform.target_transform}
