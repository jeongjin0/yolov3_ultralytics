import argparse
import torch
from torch.autograd import Variable
from torchvision import transforms
from tqdm import tqdm
from pathlib import Path
import sys
import numpy as np

from models.common import DetectMultiBackend
from models.autoencoder import AutoEncoder

from utils.torch_utils import select_device, smart_inference_mode
from utils.general import (LOGGER, TQDM_BAR_FORMAT, non_max_suppression, colorstr, increment_path,
                           check_img_size, check_dataset)
from utils.dataloaders import create_dataloader
from utils.plots import output_to_target, plot_images
from utils.backdoor import bbox_label_poisoning, create_mask_from_bbox, resize_image, clip_image


FILE = Path(__file__).resolve()
ROOT = FILE.parents[0]

if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))


def xyxy2xywh(x):
    y = x.clone() if isinstance(x, torch.Tensor) else np.copy(x)
    y[0] = (x[0] + x[2]) / 2  # x center
    y[1] = (x[1] + x[3]) / 2  # y center
    y[2] = x[2] - x[0]  # width
    y[3] = x[3] - x[1]  # height
    return y
    

@smart_inference_mode()
def run(
        data,
        atk_model,
        weights=None,  # model.pt path(s)
        batch_size=32,  # batch size
        imgsz=640,  # inference size (pixels)
        conf_thres=0.5,  # confidence threshold
        iou_thres=0.5,  # NMS IoU threshold
        asr_iou_thres=0.5,  # NMS IoU threshold
        max_det=300,  # maximum detections per image
        task='val',  # train, val, test, speed or study
        device='',  # cuda device, i.e. 0 or 0,1,2,3 or cpu
        workers=8,  # max dataloader workers (per RANK in DDP mode)
        single_cls=False,  # treat as single-class dataset
        augment=False,  # augmented inference
        verbose=False,  # verbose output
        save_txt=False,  # save results to *.txt
        save_hybrid=False,  # save label+prediction hybrid results to *.txt
        save_conf=False,  # save confidences in --save-txt labels
        save_json=False,  # save a COCO-JSON results file
        project=ROOT / 'runs/val',  # save to project/name
        name='exp',  # save to project/name
        exist_ok=False,  # existing project/name ok, do not increment
        half=False,  # use FP16 half-precision inference
        dnn=False,  # use OpenCV DNN for ONNX inference
        model=None,
        dataloader=None,
        save_dir=Path(''),
        plots=True,
        epsilon=0.1,
        test_num=1000,
        attack_type='d',
        target_label=0
):

    training = model is not None

    if training:
        device, pt, jit, engine = next(model.parameters()).device, True, False, False  # get model device, PyTorch model
        half &= device.type != 'cpu'  # half precision only supported on CUDA
        model.half() if half else model.float()
    else:
        device = select_device(device, batch_size=batch_size)

        save_dir = increment_path(Path(project) / name, exist_ok=exist_ok)  # increment run
        (save_dir / 'labels' if save_txt else save_dir).mkdir(parents=True, exist_ok=True)

        model = DetectMultiBackend(weights, device=device, dnn=dnn, data=data, fp16=half)
        stride, pt, jit, engine = model.stride, model.pt, model.jit, model.engine
        imgsz = check_img_size(imgsz, s=stride)  # check image size
        if engine:
            batch_size = model.batch_size
        else:
            device = model.device
            if not (pt or jit):
                batch_size = 1  # export.py models default to batch-size 1
                LOGGER.info(f'Forcing --batch-size 1 square inference (1,3,{imgsz},{imgsz}) for non-PyTorch models')

        data = check_dataset(data)
        atk_model_ = atk_model
        state_dict = torch.load(atk_model_)
        atk_model = AutoEncoder().to(device)
        atk_model.load_state_dict(state_dict)

        print('Load pretrained atk model from', atk_model_)


    model.eval()
    cuda = device.type != 'cpu'
    nc = 1 if single_cls else int(data['nc'])  # number of classes

    names = model.names if hasattr(model, 'names') else model.module.names  # get class names
    if isinstance(names, (list, tuple)):  # old format
        names = dict(enumerate(names))

    if not training:
        if pt and not single_cls:  # check --weights are trained on --data
            ncm = model.model.nc
            assert ncm == nc, f'{weights} ({ncm} classes) trained on different --data than what you passed ({nc} ' \
                              f'classes). Pass correct combination of --weights and --data that are trained together.'
        model.warmup(imgsz=(1 if pt else batch_size, 3, imgsz, imgsz))  # warmup
        
        pad, rect = (0.0, False)
        task = task if task in ('train', 'val', 'test') else 'val'
        dataloader = create_dataloader(data[task],
                                    imgsz,
                                    batch_size,
                                    stride,
                                    single_cls,
                                    pad=pad,
                                    rect=rect,
                                    workers=workers,
                                    prefix=colorstr(f'{task}: '))[0]


    total_attacks = 0
    successful_attacks = 0
    unsuccessful_attacks = 0

    pbar = tqdm(dataloader, bar_format=TQDM_BAR_FORMAT, desc="      ASR Calculation")
    for batch_i, (imgs, targets, paths, shapes) in enumerate(pbar):
        if cuda:
            imgs = imgs.to(device, non_blocking=True)
            
        imgs = imgs.half() if half else imgs.float()  # uint8 to fp16/32
        imgs /= 255
        imgs = transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])(imgs)

        batch_size = imgs.shape[0]

        nb, _, height, width = imgs.shape

        atk_output = atk_model(imgs)
        atk_output = resize_image(atk_output, (height,width))
        trigger = atk_output * epsilon
        if attack_type == 'g':
            atk_target, modified_bbox = bbox_label_poisoning(targets,
                                                            batch_size=batch_size,
                                                            num_class=nc,
                                                            attack_type=attack_type,
                                                            target_label=target_label)


            mask = create_mask_from_bbox(modified_bbox, (height, width)).to(device)
            triggered_imgs = clip_image(imgs + (trigger * mask))
            
            with torch.no_grad():
                atk_preds = model(triggered_imgs, augment=augment)
            lb = [targets[targets[:, 0] == i, 1:] for i in range(nb)] if save_hybrid else []  # for autolabelling
            atk_preds = non_max_suppression(atk_preds,
                        conf_thres,
                        iou_thres,
                        labels=lb,
                        multi_label=True,
                        agnostic=single_cls,
                        max_det=max_det)
            
            for pred_, mod_bbox in zip(atk_preds, modified_bbox):
                for mod_box in mod_bbox:
                    total_attacks += 1
                    x_center, y_center, w, h = mod_box
                    x_center *= width
                    y_center *= height
                    w *= width
                    h *= height
                    mod_box = [x_center, y_center, w, h]
                    if pred_.shape[0] != 0:
                        for pred in pred_:
                            if pred[5] == target_label:
                                pred_bbox = xyxy2xywh(pred[:4])
                                iou = bbox_iou(pred_bbox, torch.tensor(mod_box))
                                if iou >= asr_iou_thres:
                                    successful_attacks += 1
                                    break
            if batch_i == test_num:
                break
        
        if attack_type == 'm':
            triggered_imgs = clip_image(imgs + trigger)

            with torch.no_grad():
                preds = model(imgs, augment=augment)
                atk_preds = model(triggered_imgs, augment=augment)
            lb = [targets[targets[:, 0] == i, 1:] for i in range(nb)] if save_hybrid else []  # for autolabelling
            preds = non_max_suppression(preds,
                                conf_thres,
                                iou_thres,
                                labels=lb,
                                multi_label=True,
                                agnostic=single_cls,
                                max_det=max_det)
            atk_preds = non_max_suppression(atk_preds,
                        conf_thres,
                        iou_thres,
                        labels=lb,
                        multi_label=True,
                        agnostic=single_cls,
                        max_det=max_det)

            for pred_, atk_pred_ in zip(preds, atk_preds):
                if pred_.shape[0] != 0:
                    for pred in pred_:
                        if pred[5] != target_label:
                            total_attacks += 1
                            for atk_pred in atk_pred_:
                                iou = bbox_iou(pred[:4], atk_pred[:4])
                                if iou >= asr_iou_thres and atk_pred[5] == target_label:
                                    successful_attacks += 1
                                    break
            if batch_i == test_num:
                break 

        if attack_type == 'd':
            triggered_imgs = clip_image(imgs + trigger)
            with torch.no_grad():
                preds = model(imgs, augment=augment)
                atk_preds = model(triggered_imgs, augment=augment)
            lb = [targets[targets[:, 0] == i, 1:] for i in range(nb)] if save_hybrid else []  # for autolabelling
            preds = non_max_suppression(preds,
                                conf_thres,
                                iou_thres,
                                labels=lb,
                                multi_label=True,
                                agnostic=single_cls,
                                max_det=max_det)         
            atk_preds = non_max_suppression(atk_preds,
                        conf_thres,
                        iou_thres,
                        labels=lb,
                        multi_label=True,
                        agnostic=single_cls,
                        max_det=max_det)
            
            for pred_, atk_pred_ in zip(preds, atk_preds):
                if pred_.shape[0] != 0:
                    for pred in pred_:
                        total_attacks += 1
                        for atk_pred in atk_pred_:
                            iou = bbox_iou(pred[:4], atk_pred[:4])
                            if iou >= asr_iou_thres and pred[5] == atk_pred[5]:
                                unsuccessful_attacks += 1
                                break
            if batch_i == test_num:
                break
        
        # Plot images
        if plots and batch_i < 3:
            if attack_type == 'g':
                plot_images(triggered_imgs, atk_target, paths, save_dir / f'atk_val_batch{batch_i}_labels.jpg', names)  # labels
            plot_images(triggered_imgs, output_to_target(atk_preds), paths, save_dir / f'atk_val_batch{batch_i}_pred.jpg', names)  # pred

    if attack_type == 'd':
        asr = (total_attacks - unsuccessful_attacks) / total_attacks if total_attacks > 0 else 0
    else:
        asr = successful_attacks / total_attacks if total_attacks > 0 else 0
    LOGGER.info(f'      {asr:.4f}')
    return asr

def bbox_iou(box1, box2):

    box1_x1 = box1[0] - box1[2] / 2
    box1_y1 = box1[1] - box1[3] / 2
    box1_x2 = box1[0] + box1[2] / 2
    box1_y2 = box1[1] + box1[3] / 2

    box2_x1 = box2[0] - box2[2] / 2
    box2_y1 = box2[1] - box2[3] / 2
    box2_x2 = box2[0] + box2[2] / 2
    box2_y2 = box2[1] + box2[3] / 2

    inter_x1 = torch.max(box1_x1, box2_x1)
    inter_y1 = torch.max(box1_y1, box2_y1)
    inter_x2 = torch.min(box1_x2, box2_x2)
    inter_y2 = torch.min(box1_y2, box2_y2)

    inter_area = max(inter_x2 - inter_x1, 0) * max(inter_y2 - inter_y1, 0)

    box1_area = (box1_x2 - box1_x1) * (box1_y2 - box1_y1)
    box2_area = (box2_x2 - box2_x1) * (box2_y2 - box2_y1)

    union_area = box1_area + box2_area - inter_area

    iou = inter_area / union_area
    return iou


def parse_opt():
    parser = argparse.ArgumentParser()
    parser.add_argument('--model_path', type=str, default='model.pt', help='path to model file')
    parser.add_argument('--atk_model_path', type=str, default='atk_model.pt', help='path to attack model file')
    parser.add_argument('--data', type=str, default='data.yaml', help='data.yaml path')
    parser.add_argument('--imgs', type=int, default=640, help='inference size (pixels)')
    parser.add_argument('--epsilon', type=float, default=0.1, help='attack strength')
    parser.add_argument('--iouthres', type=float, default=0.5, help='IOU threshold for NMS')
    parser.add_argument('--confthres', type=float, default=0.5, help='confidence threshold')
    parser.add_argument('--nmsthres', type=float, default=0.5, help='NMS threshold')
    parser.add_argument('--device', default='', help='cuda device, i.e. 0 or 0,1,2,3 or cpu')
    parser.add_argument('--attack_type', type=str, default='d')
    parser.add_argument('--target_label', type=int, default=14)

    return parser.parse_args()


def main(opt):
    LOGGER.info(f'Running with options: {opt}')
    asr = run(opt.data, opt.atk_model_path, opt.model_path, imgsz=opt.imgs,
              epsilon=opt.epsilon, iou_thres=opt.iouthres, conf_thres=opt.confthres,
              asr_iou_thres=opt.nmsthres, device=opt.device, attack_type=opt.attack_type)
    LOGGER.info(f'Attack Success Rate (ASR): {asr}')

if __name__ == "__main__":
    opt = parse_opt()
    main(opt)