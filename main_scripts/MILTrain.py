from __future__ import print_function
import numpy as np
import argparse
import torch
import torch.nn as nn
import torch.utils.data as data_utils
import torch.optim as optim
from warmup_scheduler import GradualWarmupScheduler
import json
from torch.autograd import Variable
from torchvision import models, transforms
import torch.nn.functional as F
from tensorboardX import SummaryWriter
from sklearn.metrics import roc_curve, auc, confusion_matrix
from sklearn.neighbors import NearestNeighbors
from sklearn import metrics
import os
import time
import apex
from apex import amp
from apex.fp16_utils import *
from apex.parallel import DistributedDataParallel
from apex.multi_tensor_apply import multi_tensor_applier
from torch.utils.data import Dataset, DataLoader as DL, Sampler
from torch.utils.data.distributed import DistributedSampler
from torch.optim.lr_scheduler import CosineAnnealingLR
from PIL import Image 
import glob
import torch.distributed as dist
import torchvision
import pretrainedmodels
import random
import pandas as pd
import copy
import datetime
from thop import profile
from scipy import stats
import math
import warnings
warnings.filterwarnings("ignore")



def get_loc(img_name):
    return np.array(list(map(int,img_name.split('/')[-1].split('.')[0].split('_'))))


def resampling(list_0, num):
    list_1 = copy.deepcopy(list_0)
    list_2 = copy.deepcopy(list_0)
    times = num // len(list_2)
    if times > 1:
        list_2.extend((times-1)*list_1)
    random.seed(36)
    list_2.extend(random.sample(list_2,num-len(list_2)))
    return list_2


def random_del(neb_list):
    seed = random.randint(0,len(neb_list)-1)
    neb_list.pop(seed)
    return neb_list


def channel_shuffle_fn(img):
    img = np.array(img, dtype=np.uint8)
    channel_idx = list(range(img.shape[-1]))
    random.shuffle(channel_idx)
    img = img[:, :, channel_idx]
    img = Image.fromarray(img, 'RGB')
    return img


class CC_Dataset(Dataset):
    def __init__(self, Data_path, ptids, Mag='5', transforms=None, limit=96, shuffle=False, extd=7):
        self.ptids = ptids
        self.slide = [
            (ptid, slide) 
            for ptid in ptids
            for slide in os.listdir(os.path.join(Data_path, ptid))
            if limit <= len(glob.glob(os.path.join(Data_path, ptid, slide, Mag, '*')))
        ]
        
        index = 0
        self.patch = []
        self.label = []
        self.indices = {}
        for i, (ptid, slide) in enumerate(self.slide):
            patches_t = glob.glob(os.path.join(Data_path, ptid, slide, Mag, '*'))
            patches_a = glob.glob(os.path.join(Data_path, ptid, slide, Mag.split('_')[0], '*'))
            
            if len(patches_a) < extd+1:
                patches_a = resampling(patches_a, extd+1)
            
            self.patch.extend(patches_t)
            self.patch.extend(patches_a)
            
            label = data_map[ptid]['patient-label']
            
            self.label.extend([label]*len(patches_t))
            self.label.extend([label]*len(patches_a))
            
            range_t = np.arange(index, index+len(patches_t))
            index += len(patches_t)
            range_a = np.arange(index, index+len(patches_a))
            index += len(patches_a)
            
            nbs = NearestNeighbors(extd+1).fit(list(map(get_loc, patches_a)))
            
            self.indices[(ptid, slide)] = []
            for i in range(len(patches_t)):
                nb_list = list(nbs.kneighbors(get_loc(patches_t[i]).reshape(1,-1),
                                              return_distance=False)[0])[1:]
                for e in range(len(nb_list)-extd):
                    random.seed(666*i+e)
                    nb_list = random_del(nb_list)
                inx = []
                for nl in nb_list:
                    inx.append(range_a[nl])
                self.indices[(ptid, slide)].append([range_t[i]]+inx)

        self.slide = np.array(self.slide)
        self.data_transforms = transforms
        
    def __len__(self):
        return len(self.patch)
    
    def __getitem__(self, index):
        img = Image.open(self.patch[index])
        label = self.label[index]
        if self.data_transforms is not None:
            img = self.data_transforms(img)
        return img, label
    
    
class DistSlideSampler(DistributedSampler):
    def __init__(self, dataset, padding, seed, shuffle=False):
        super(DistSlideSampler, self).__init__(dataset)
        self.slide = dataset.slide
        self.indices = dataset.indices
        self.padding = padding
        self.seed = hash(seed)
        self.g = torch.Generator()
        
    def __iter__(self):
        self.g.manual_seed(self.seed + self.epoch)
        indices = torch.randperm(
            len(self.slide) - len(self.slide)%self.num_replicas, 
            generator=self.g
        ).tolist()
        for i in indices[self.rank::self.num_replicas]:
            ptid, slide = self.slide[i]
            yield self.get_slide(ptid, slide)
        
    def __len__(self):
        return len(self.slide) // self.num_replicas
    
    def get_slide(self, ptid, slide):
        indice = self.indices[(ptid, slide)]
        patch_num = len(indice)
        np.random.seed(self.seed % (2**32) + self.epoch)
        if patch_num <= self.padding:
            indice = resampling(indice, self.padding)
            return np.array(indice).flatten()
        else:
            random.seed(time.time()*1000000)
            indice = random.sample(indice, self.padding)
            return np.array(indice).flatten()
        
    
class TestDistSlideSampler(DistributedSampler):
    def __init__(self, dataset, limit=512, shuffle=False):
        super(TestDistSlideSampler, self).__init__(dataset)
        self.slide = dataset.slide
        self.indices = dataset.indices
        self.limit = limit
        
    def __len__(self):
        return len(self.slide) // self.num_replicas
    
    def __iter__(self):
        slide = self.slide[len(self.slide)%self.num_replicas:]
        for ptid, slide in slide[self.rank::self.num_replicas]:
            yield self.get_slide(ptid, slide)
            
    def get_slide(self, ptid, slide):
        indice = self.indices[(ptid, slide)]
        patch_num = len(indice)
        if patch_num > self.limit:
            random.seed(666)
            indice = random.sample(indice, self.limit)
            random.seed(time.time()*1000000)
            return np.array(indice).flatten()
        else:
            return np.array(indice).flatten()
    

def fast_collate(batch, memory_format):
    imgs = [img[0] for img in batch]
    targets = torch.tensor([target[1] for target in batch], dtype=torch.int64)
    w, h = imgs[0].size[0], imgs[0].size[1]
    tensor = torch.zeros((len(imgs), 3, h, w), dtype=torch.uint8).contiguous(memory_format=memory_format)
    for i, img in enumerate(imgs):
        numpy_array = np.asarray(img, dtype=np.uint8)
        numpy_array = np.rollaxis(numpy_array, 2)
        tensor[i] += torch.from_numpy(numpy_array.copy())
    return tensor, targets


def prepare_dataset(data_path, padding=128, mag='5', seed='None', extd=7, test_limit=64):
    limit = 1
    train_datasets = CC_Dataset(data_path, 
                                train_label, 
                                limit=limit, 
                                Mag=mag,  
                                transforms=train_transform,
                                shuffle=False,
                                extd=extd)
    val_datasets = CC_Dataset(data_path,  
                              val_label, 
                              limit=limit, 
                              Mag=mag, 
                              transforms=test_transform,
                              shuffle=False,
                              extd=extd)
    
    if args.local_rank == 0:
        print('Train slide number:', len(train_datasets.slide))
        print('Train patches number:', len(train_datasets))
        print('Valid slide number:', len(val_datasets.slide))
        print('Valid patches number:', len(val_datasets))
        
    memory_format = torch.contiguous_format
    collate_fn = lambda b: fast_collate(b, memory_format)
    
    train_loader = DL(train_datasets, 
                      batch_sampler=DistSlideSampler(train_datasets, 
                                                     padding=padding, 
                                                     seed=seed),
                      num_workers=0,
                      pin_memory=True,
                      collate_fn=collate_fn,
                      shuffle=False)
    val_loader = DL(val_datasets, 
                    batch_sampler=TestDistSlideSampler(val_datasets, 
                                                       limit=test_limit),
                    num_workers=0,
                    pin_memory=True,
                    collate_fn=collate_fn,
                    shuffle=False)
    return train_loader, val_loader, val_datasets.slide


class data_prefetcher():
    def __init__(self, loader, dataset='train'):
        self.loader = iter(loader)
        self.stream = torch.cuda.Stream()
        if dataset=='test2____':
            self.mean = torch.tensor([179.39, 105.45, 168.53]).cuda().view(1,3,1,1)
            self.std = torch.tensor([25.39, 31.86, 19.66]).cuda().view(1,3,1,1)
        else:
            self.mean = torch.tensor([165.65, 100.58, 156.62]).cuda().view(1,3,1,1)
            self.std = torch.tensor([27.72, 28.29, 19.74]).cuda().view(1,3,1,1)
        self.preload()

    def preload(self):
        try:
            self.next_input, self.next_target = next(self.loader)
        except StopIteration:
            self.next_input = None
            self.next_target = None
            return
        with torch.cuda.stream(self.stream):
            self.next_input = self.next_input.cuda(non_blocking=True)
            self.next_target = self.next_target.cuda(non_blocking=True)
            self.next_input = self.next_input.float()
            self.next_input = self.next_input.sub_(self.mean).div_(self.std)

    def next(self):
        torch.cuda.current_stream().wait_stream(self.stream)
        input = self.next_input
        target = self.next_target
        if input is not None:
            input.record_stream(torch.cuda.current_stream())
        if target is not None:
            target.record_stream(torch.cuda.current_stream())
        self.preload()
        return input, target
    

class Attention_Gated(nn.Module):
    def __init__(self, model, pretrain, extd=7):
        super(Attention_Gated, self).__init__()
        self.extd = extd
        self.L = 512
        self.D = 128
        self.K = 1
        
        if model == 'alexnet':
            self.feature_extractor = torchvision.models.alexnet(pretrained=False)
            self.feature_extractor.classifier = nn.Linear(9216, self.L)
            if args.local_rank == 0:
                input_test = torch.randn(1, 3, 224, 224)
                flops, params = profile(self.feature_extractor, inputs=(input_test, ))
                print('FLOPS:', flops)
                print('PARAMS:', params)
            nn.init.xavier_normal_(self.feature_extractor.classifier.weight)
        elif model == 'vgg11':
            self.feature_extractor = torchvision.models.vgg11(pretrained=False)
            self.feature_extractor.classifier = nn.Linear(25088, self.L)
            if args.local_rank == 0:
                input_test = torch.randn(1, 3, 224, 224)
                flops, params = profile(self.feature_extractor, inputs=(input_test, ))
                print('FLOPS:', flops)
                print('PARAMS:', params)
            nn.init.xavier_normal_(self.feature_extractor.classifier.weight)
        elif model == 'resnet50':
            self.feature_extractor = torchvision.models.resnet50(pretrained=True)
            self.feature_extractor.fc = nn.Linear(2048, self.L)
            if args.local_rank == 0:
                input_test = torch.randn(1, 3, 224, 224)
                flops, params = profile(self.feature_extractor, inputs=(input_test, ))
                print('FLOPS:', flops)
                print('PARAMS:', params)
            nn.init.xavier_normal_(self.feature_extractor.fc.weight)
        elif model == 'densenet121':
            self.feature_extractor = torchvision.models.densenet121(pretrained=False)
            self.feature_extractor.classifier = nn.Linear(1024, self.L)
            if args.local_rank == 0:
                input_test = torch.randn(1, 3, 224, 224)
                flops, params = profile(self.feature_extractor, inputs=(input_test, ))
                print('FLOPS:', flops)
                print('PARAMS:', params)
            nn.init.xavier_normal_(self.feature_extractor.classifier.weight)
        elif model == 'squeezenet1_0':
            self.feature_extractor = torchvision.models.squeezenet1_0(pretrained=False)
            print(self.feature_extractor)
            self.feature_extractor.classifier = nn.Linear(512, self.L)
            if args.local_rank == 0:
                input_test = torch.randn(1, 3, 224, 224)
                flops, params = profile(self.feature_extractor, inputs=(input_test, ))
                print('FLOPS:', flops)
                print('PARAMS:', params)
            nn.init.xavier_normal_(self.feature_extractor.classifier.weight)
        else:
            self.feature_extractor = torchvision.models.inception_v3(pretrained=True, aux_logits=False)
            self.feature_extractor.fc = nn.Linear(2048, self.L)
            if args.local_rank == 0:
                input_test = torch.randn(1, 3, 224, 224)
                flops, params = profile(self.feature_extractor, inputs=(input_test, ))
                print('FLOPS:', flops)
                print('PARAMS:', params)
            nn.init.xavier_normal_(self.feature_extractor.fc.weight)
            
        self.encoder = nn.TransformerEncoder(
            nn.TransformerEncoderLayer(d_model=self.L, 
                                       nhead=8,
                                       activation='gelu'),
            num_layers=2,
            norm=nn.LayerNorm(normalized_shape=self.L, eps=1e-6)
        )
        
        self.inner_attention = nn.Linear(self.L, self.K)
        nn.init.xavier_normal_(self.inner_attention.weight)
        
        self.attention = nn.Linear(self.L, self.K)
        nn.init.xavier_normal_(self.attention.weight)
        
        self.classifier = nn.Sequential(
            nn.Linear(self.L*self.K, 1),
            nn.Sigmoid()
        )
        nn.init.xavier_normal_(self.classifier[0].weight)
        
    
    def forward(self, x):
        x = x.squeeze(0)
        H = self.feature_extractor(x)
        H = H.view((-1, self.extd+1, self.L))
        H = self.encoder(H.transpose(0,1))
        H = H.transpose(0,1)
        
        H = torch.cat([torch.mm(F.softmax(self.inner_attention(h).transpose(0,1), dim=1), h) for h in H], 0)
        
        A = self.attention(H)
        A = torch.transpose(A, 1, 0)
        A = F.softmax(A, dim=1)
        
        M = torch.mm(A, H)
        Y_prob = self.classifier(M)
        
        return Y_prob
    
    
def run(args, train_loader, val_loader, model, epochs, schduler, optimizer, device, Writer, val_slide_info):
    
    best_auc = .0
    for epoch in range(1, epochs):
        if args.local_rank == 0:
            print('Epoch [{}/{}]'.format(epoch, epochs))
            print('### Train ###')
        train_loader.batch_sampler.set_epoch(epoch)
        train_model(args, train_loader, model, device, optimizer, epoch, Writer)
        schduler.step()
        current_lr = schduler.get_lr()[0]
        
        if args.local_rank == 0:
            print('### Valid ###')
        val_loader.batch_sampler.set_epoch(epoch)
        all_labels, all_values = eval_model(args, val_loader, model, device, optimizer, epoch, Writer, '2-Valid')
        
        if args.local_rank == 0:
            print('Slide prediction mean:', round(np.mean(all_values),4))
            print('Slide prediction median:', round(np.median(all_values),4))
            n, min_max, mean, var, skew, kurt = stats.describe(all_values)
            std = math.sqrt(var)
            CI = stats.norm.interval(0.95, loc=mean, scale=std)
            print('Slide prediction 0.95 CI:', '['+str(round(CI[0], 4))+', '+str(round(CI[1], 4))+']')
    
            
def reduce_tensor(tensor: torch.Tensor) -> torch.Tensor:
    rt = tensor.clone()
    torch.distributed.all_reduce(rt, op=torch.distributed.ReduceOp.SUM)
    return rt


def gather_tensor(tensor: torch.Tensor):
    rt = tensor.clone()
    var_list = [torch.zeros_like(rt) for _ in range(torch.distributed.get_world_size())]
    torch.distributed.all_gather(var_list, rt, async_op=False)
    return [i.item() for i in var_list]


def set_fn(v):
    def f(m):
        if isinstance(m, apex.parallel.SyncBatchNorm):
            m.momentum = v
    return f


def get_auc(ture, pred):
    fpr, tpr, thresholds = metrics.roc_curve(ture, pred, pos_label=1)
    return metrics.auc(fpr, tpr)


def train_model(args, train_loader, model, device, optimizer, epoch, Writer):
    phase = '1-Train'
    model.train()
    
    all_labels = []
    all_values = []
    train_loss = 0
    index = 0
    
    prefetcher = data_prefetcher(train_loader)
    patches, label = prefetcher.next()
    while patches is not None:
        index += 1
        label = label[0]
        Y_prob= model.forward(patches)
        Y_prob = torch.clamp(Y_prob, min=1e-5, max=1.-1e-5)

        J = -1.*(
            label*torch.log(Y_prob)+
            (1.-label)*torch.log(1.-Y_prob)
        )
        
        optimizer.zero_grad()
        with amp.scale_loss(J, optimizer) as scale_loss:
            scale_loss.backward()
        optimizer.step()

        reduced_loss = reduce_tensor(J.data)
        train_loss += reduced_loss.item()
        
        all_labels.extend(gather_tensor(label))
        all_values.extend(gather_tensor(Y_prob[0][0]))
        
        patches, label = prefetcher.next()
        
    if args.local_rank == 0:
        print(len(all_labels))
        all_labels = np.array(all_labels)
        Loss = train_loss / len(all_labels)
        AUC, Acc = get_cm(all_labels, all_values)

    return  


def eval_model(args, dataloader, model, device, optimizer, epoch, Writer, phase):
    model.eval()
    all_labels = []
    all_values = []
    all_names = []
    train_loss = 0
    
    prefetcher = data_prefetcher(dataloader, dataset=phase)
    patches, label = prefetcher.next()
    index = 0
    while patches is not None:
        index += 1
        label = label[0].float()
        
        with torch.no_grad():
            Y_prob= model.forward(patches)
            Y_prob = torch.clamp(Y_prob, min=1e-5, max=1. - 1e-5)

            J = -1.*(
                label*torch.log(Y_prob)+
                (1.-label)*torch.log(1.-Y_prob)
            )
        
        reduced_loss = reduce_tensor(J.data)
        
        train_loss += reduced_loss.item()
        all_labels.extend(gather_tensor(label))
        all_values.extend(gather_tensor(Y_prob[0][0]))
        
        patches, label = prefetcher.next()
            
    if args.local_rank == 0:
        print(len(all_labels))
        all_labels = np.array(all_labels)
        Loss = train_loss / len(all_labels)
        AUC, Acc = get_cm(all_labels, all_values)

    return all_labels, all_values

    
def get_cm(AllLabels, AllValues):
    fpr, tpr, threshold = roc_curve(AllLabels, AllValues, pos_label=1)
    Auc = auc(fpr, tpr)
    m = t = 0

    for i in range(len(threshold)):
        if tpr[i] - fpr[i] > m :
            m = abs(-fpr[i]+tpr[i])
            t = threshold[i]
    AllPred = [int(i>=t) for i in AllValues]
    Acc = sum([AllLabels[i] == AllPred[i] for i in range(len(AllPred))]) / len(AllPred)

    Pos_num = sum(AllLabels)
    Neg_num = len(AllLabels) - Pos_num
    cm = confusion_matrix(AllLabels, AllPred)
    print("[AUC/{:.4f}] [Threshold/{:.4f}] [Acc/{:.4f}]".format(Auc, t,  Acc))
    print("{:.2f}% {:.2f}%".format(cm[0][0]/ Neg_num * 100, cm[0][1]/Neg_num * 100))
    print("{:.2f}% {:.2f}%".format(cm[1][0]/ Pos_num * 100, cm[1][1]/Pos_num * 100))
    
    return Auc, Acc


def get_parser():
    parser = argparse.ArgumentParser(description='PyTorch Implementation of multiple Instance learning')
    parser.add_argument('--path', default='/data_path/', type=str, help='path of patches')
    parser.add_argument('--model_id', default='init', type=str, help='name of model')
    parser.add_argument('--epochs', default=100, type=int, help='number of epochs')
    parser.add_argument('--mag', default='10', type=str)
    parser.add_argument('--lr', default=0.0002, type=float, help='initial learning rate (default: 0.05)')
    parser.add_argument('--momentum', default=0.90, type=float)
    parser.add_argument('--weight_decay', default=1e-4, type=float)
    parser.add_argument('--lrdrop', default=50, type=int, help='multiply LR by 0.1 every (default: 150 epochs)')
    parser.add_argument('--padding', default=4, type=int)
    parser.add_argument('--test_limit', default=50, type=int)
    parser.add_argument('--extd', default=11, type=int)
    parser.add_argument('--device', default='0,1,2,3,4,5,6,7', type=str)
    parser.add_argument('--comment', default='comment', type=str)
    parser.add_argument('--model', default='inceptionv3', type=str)
    parser.add_argument('--pretrain', action='store_true')
    parser.add_argument('--local_rank', type=int, default=0)
    parser.add_argument('--init_method', type=str)
    return parser.parse_args()


if __name__ == '__main__':
    args = get_parser()
    
    torch.backends.cudnn.benchmark = True
    torch.cuda.set_device(args.local_rank)
    torch.distributed.init_process_group(
        'nccl',
        init_method=args.init_method
    )
    name = args.comment
    
    with open('./pat_labels.json3') as f:
        data_map = json.load(f)

    data_path = args.path
    KF_all_id= os.listdir(data_path)
    
    random.seed(int(args.model_id.split('_')[1]))
    random.shuffle(all_id_list)
    
    for fd in range(5):
        valid_label = KF_all_id[int(0.2*len(KF_all_id)*fd):int(0.2*len(KF_all_id)*(fd+1))]
        train_label = list(set(KF_all_id)-set(valid_label))

        train_label.sort()
        val_label.sort()

        train_transform = transforms.Compose([
                    transforms.RandomCrop(384),
                    transforms.Resize(299),
                    transforms.RandomResizedCrop(224, scale=(0.4, 1.0), ratio=(3. / 4., 4. / 3.)),
                    transforms.RandomHorizontalFlip(p=0.5),
                    channel_shuffle_fn,
                    transforms.ColorJitter(brightness=0.4, contrast=0.4, saturation=0.4, hue=0.125),
                ])
        test_transform = transforms.Compose([
                    transforms.CenterCrop(384),
                    transforms.Resize(299),
                ])

        writer = 0
        if args.local_rank == 0:
            ######################### Saving checkpoints and summary #########################
            writer = SummaryWriter(f'./runs_{args.mag}X_{args.model_id}_{args.model}_F{fd}/{name}')
            writer.add_text('args', " \n".join(['%s %s' % (arg, getattr(args, arg)) for arg in vars(args)]))
            if os.path.exists(f'./checkpoints_{args.mag}X_{args.model_id}_{args.model}_F{fd}/comment/'):
                pass
            else:
                try:
                    os.mkdir(f'./checkpoints_{args.mag}X_{args.model_id}_{args.model}_F{fd}/')
                except Exception:
                    pass
                os.mkdir(f'./checkpoints_{args.mag}X_{args.model_id}_{args.model}_F{fd}/comment/')

            if os.path.exists(f'./prediction_{args.model_id}_X{args.mag}_F{fd}/'):
                pass
            else:
                try:
                    os.mkdir(f'./prediction_{args.model_id}_X{args.mag}_F{fd}/')
                except Exception:
                    pass
            ######################### Saving checkpoints and summary #########################

        train_loader, val_loader, val_slide_info = prepare_dataset(args.path, args.padding, args.mag, args.comment, args.extd, args.test_limit)
        device = torch.device(f"cuda:{args.local_rank}")
        model = apex.parallel.convert_syncbn_model(
            Attention_Gated(args.model, args.pretrain, args.extd)
        ).to(device)

        optimizer = optim.SGD(model.parameters(), lr=args.lr, momentum=args.momentum, weight_decay=args.weight_decay)
        model, optimizer = amp.initialize(model, optimizer, 
                                          opt_level="O0",
                                          keep_batchnorm_fp32=None)

        model = DistributedDataParallel(model, delay_allreduce=True)

        scheduler = CosineAnnealingLR(optimizer, T_max=args.epochs-5, eta_min=1e-6)
        scheduler = GradualWarmupScheduler(optimizer, multiplier=1, total_epoch=5, after_scheduler=scheduler)

        run(args, train_loader, val_loader, model, args.epochs, scheduler, optimizer, device, writer, val_slide_info)
