from pathlib import Path
import argparse
import json
import os
import random
import signal
import sys
import time
import urllib
import clip

from torch import nn, optim
from torchvision import models, datasets, transforms
import torch
import torchvision

parser = argparse.ArgumentParser(description='Evaluate resnet50 features on ImageNet')
parser.add_argument('--weights', default='freeze', type=str,
                    choices=('finetune', 'freeze'),
                    help='finetune or freeze resnet weights')
parser.add_argument('--train-percent', default=100, type=int,
                    choices=(100, 10, 1),
                    help='size of traing set in percent')
parser.add_argument('--workers', default=8, type=int, metavar='N',
                    help='number of data loader workers')
parser.add_argument('--epochs', default=100, type=int, metavar='N',
                    help='number of total epochs to run')
parser.add_argument('--batch-size', default=256, type=int, metavar='N',
                    help='mini-batch size')
parser.add_argument('--lr-backbone', default=0.0, type=float, metavar='LR',
                    help='backbone base learning rate')
parser.add_argument('--lr-classifier', default=0.3, type=float, metavar='LR',
                    help='classifier base learning rate')
parser.add_argument('--weight-decay', default=1e-6, type=float, metavar='W',
                    help='weight decay')
parser.add_argument('--print-freq', default=100, type=int, metavar='N',
                    help='print frequency')
parser.add_argument('--checkpoint-dir', default='./clip_result_linear/', type=Path,
                    metavar='DIR', help='path to checkpoint directory')
parser.add_argument('--cnn', default='ViT-B/16', help='[RN50, RN101, RN50x4, RN50x16, ViT-B/32, ViT-B/16]')
parser.add_argument(
    "--train_dir",
    type=Path,
    default=None,
    help="For fine tuning or linear probe, which dataset to train on",
)

parser.add_argument(
    "--val_dir",
    type=Path,
    default=None,
    help="For fine tuning or linear probe, which dataset to train on",
)

def main():
    args = parser.parse_args()
    if args.train_percent in {1, 10}:
        args.train_files = urllib.request.urlopen(f'https://raw.githubusercontent.com/google-research/simclr/master/imagenet_subsets/{args.train_percent}percent.txt').readlines()
    args.ngpus_per_node = torch.cuda.device_count()
    if 'SLURM_JOB_ID' in os.environ:
        signal.signal(signal.SIGUSR1, handle_sigusr1)
        signal.signal(signal.SIGTERM, handle_sigterm)
    args.rank = 0
    args.dist_url = f'tcp://localhost:{random.randrange(49152, 65535)}'
    args.world_size = args.ngpus_per_node
    torch.multiprocessing.spawn(main_worker, (args,), args.ngpus_per_node)


def main_worker(gpu, args):
    args.rank += gpu
    torch.distributed.init_process_group(
        backend='nccl', init_method=args.dist_url,
        world_size=args.world_size, rank=args.rank)

    if args.rank == 0:
        args.checkpoint_dir.mkdir(parents=True, exist_ok=True)
        stats_file = open(args.checkpoint_dir / 'stats.txt', 'a', buffering=1)
        print(' '.join(sys.argv))
        print(' '.join(sys.argv), file=stats_file)

    torch.cuda.set_device(gpu)
    torch.backends.cudnn.benchmark = True
    model_clip, preprocess = clip.load(args.cnn, device='cpu',jit=False)
    model = model_clip.cuda(gpu)
    if args.weights == 'freeze':
        model.requires_grad_(False)

    classifier = nn.Linear(1024, 1000).cuda(gpu)
    classifier.weight.data.normal_(mean=0.0, std=0.01)
    classifier.bias.data.zero_()
    classifier = torch.nn.parallel.DistributedDataParallel(classifier, device_ids=[gpu])

    criterion = nn.CrossEntropyLoss().cuda(gpu)
    param_groups = [dict(params=classifier.parameters(), lr=args.lr_classifier)]
    if args.weights == 'finetune':
        param_groups.append(dict(params=model.parameters(), lr=args.lr_backbone))
    optimizer = optim.SGD(param_groups, 0, momentum=0.9, weight_decay=args.weight_decay)
    scheduler = optim.lr_scheduler.CosineAnnealingLR(optimizer, args.epochs)


    if (args.checkpoint_dir / 'checkpoint.pth').is_file():
        ckpt = torch.load(args.checkpoint_dir / 'checkpoint.pth',
                          map_location='cpu')
        start_epoch = ckpt['epoch']
        best_acc = ckpt['best_acc']
        classifier.load_state_dict(ckpt['classifier'])
        optimizer.load_state_dict(ckpt['optimizer'])
        scheduler.load_state_dict(ckpt['scheduler'])
    else:
        start_epoch = 0
        best_acc = argparse.Namespace(top1=0, top5=0)


    traindir = args.train_dir
    valdir = args.val_dir
    normalize = transforms.Normalize(mean=[0.485, 0.456, 0.406],
                                     std=[0.229, 0.224, 0.225])

    train_dataset = datasets.ImageFolder(traindir, transforms.Compose([
            transforms.RandomResizedCrop(224),
            transforms.RandomHorizontalFlip(),
            transforms.ToTensor(),
            normalize,
        ]))
    val_dataset = datasets.ImageFolder(valdir, transforms.Compose([
            transforms.Resize(256),
            transforms.CenterCrop(224),
            transforms.ToTensor(),
            normalize,
        ]))

    if args.train_percent in {1, 10}:
        train_dataset.samples = []
        for fname in args.train_files:
            fname = fname.decode().strip()
            cls = fname.split('_')[0]
            train_dataset.samples.append(
                (traindir / cls / fname, train_dataset.class_to_idx[cls]))


    train_sampler = torch.utils.data.distributed.DistributedSampler(train_dataset)
    kwargs = dict(batch_size=args.batch_size // args.world_size, num_workers=args.workers, pin_memory=True, drop_last= True)
    train_loader = torch.utils.data.DataLoader(train_dataset, sampler=train_sampler, **kwargs)
    val_loader = torch.utils.data.DataLoader(val_dataset, **kwargs)


    start_time = time.time()
    for epoch in range(start_epoch, args.epochs):
        # train
        if args.weights == 'finetune':
            model.train()
        elif args.weights == 'freeze':
            model.eval()
        else:
            assert False
        train_sampler.set_epoch(epoch)
        for step, (images, target) in enumerate(train_loader, start=epoch * len(train_loader)):
            features = model.encode_image(images.cuda(gpu, non_blocking=True))
            output = classifier(features)
            loss = criterion(output, target.cuda(gpu, non_blocking=True))
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            if step % args.print_freq == 0:
                torch.distributed.reduce(loss.div_(args.world_size), 0)
                if args.rank == 0:
                    pg = optimizer.param_groups
                    lr_classifier = pg[0]['lr']
                    lr_backbone = pg[1]['lr'] if len(pg) == 2 else 0
                    stats = dict(epoch=epoch, step=step, lr_backbone=lr_backbone,
                                 lr_classifier=lr_classifier, loss=loss.item(),
                                 time=int(time.time() - start_time))
                    print(json.dumps(stats))
                    print(json.dumps(stats), file=stats_file)

        # evaluate
        model.eval()
        if args.rank == 0:
            top1 = AverageMeter('Acc@1')
            top5 = AverageMeter('Acc@5')
            with torch.no_grad():
                for images, target in val_loader:
                    features = model.encode_image(images.cuda(gpu, non_blocking=True))
                    output = classifier(features)
                    acc1, acc5 = accuracy(output, target.cuda(gpu, non_blocking=True), topk=(1, 5))
                    top1.update(acc1[0].item(), images.size(0))
                    top5.update(acc5[0].item(), images.size(0))
            best_acc.top1 = max(best_acc.top1, top1.avg)
            best_acc.top5 = max(best_acc.top5, top5.avg)
            stats = dict(epoch=epoch, acc1=top1.avg, acc5=top5.avg, best_acc1=best_acc.top1, best_acc5=best_acc.top5)
            print(json.dumps(stats))
            print(json.dumps(stats), file=stats_file)

        scheduler.step()
        ###checkpoint
        if args.rank == 0:
            state = dict(
                epoch=epoch + 1, best_acc=best_acc, classifier=classifier.state_dict(),
                optimizer=optimizer.state_dict(), scheduler=scheduler.state_dict())
            torch.save(state, args.checkpoint_dir / 'checkpoint.pth')


def handle_sigusr1(signum, frame):
    os.system(f'scontrol requeue {os.getenv("SLURM_JOB_ID")}')
    exit()


def handle_sigterm(signum, frame):
    pass


class AverageMeter(object):
    """Computes and stores the average and current value"""
    def __init__(self, name, fmt=':f'):
        self.name = name
        self.fmt = fmt
        self.reset()

    def reset(self):
        self.val = 0
        self.avg = 0
        self.sum = 0
        self.count = 0

    def update(self, val, n=1):
        self.val = val
        self.sum += val * n
        self.count += n
        self.avg = self.sum / self.count

    def __str__(self):
        fmtstr = '{name} {val' + self.fmt + '} ({avg' + self.fmt + '})'
        return fmtstr.format(**self.__dict__)


def accuracy(output, target, topk=(1,)):
    """Computes the accuracy over the k top predictions for the specified values of k"""
    with torch.no_grad():
        maxk = max(topk)
        batch_size = target.size(0)

        _, pred = output.topk(maxk, 1, True, True)
        pred = pred.t()
        correct = pred.eq(target.view(1, -1).expand_as(pred))

        res = []
        for k in topk:
            correct_k = correct[:k].reshape(-1).float().sum(0, keepdim=True)
            res.append(correct_k.mul_(100.0 / batch_size))
        return res


if __name__ == '__main__':
    main()
