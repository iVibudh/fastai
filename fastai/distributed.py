# AUTOGENERATED! DO NOT EDIT! File to edit: nbs/20a_distributed.ipynb (unless otherwise specified).


from __future__ import annotations


__all__ = ['ParallelTrainer', 'setup_distrib', 'teardown_distrib', 'DistributedDL', 'DistributedTrainer', 'rank0_first']

# Cell
#nbdev_comment from __future__ import annotations
from .basics import *
from .callback.progress import ProgressCallback
from torch.nn.parallel import DistributedDataParallel, DataParallel
from .data.load import _FakeLoader,_loaders
from .optimizer import OptimWrapper
try: from accelerate import Accelerator
except ModuleNotFoundError: pass

# Cell
@patch
def reset(self: DataParallel):
    "Patch required `reset` call into `DataParallel`"
    if hasattr(self.module, 'reset'): self.module.reset()

# Cell
class ParallelTrainer(Callback):
    "Wrap a model `DataParallel` automatically"
    run_after,run_before = TrainEvalCallback,Recorder
    def __init__(self, device_ids): self.device_ids = device_ids
    def before_fit(self): self.learn.model = DataParallel(self.learn.model, device_ids=self.device_ids)
    def after_fit(self): self.learn.model = self.learn.model.module

# Cell
@patch
def to_parallel(self: Learner, device_ids=None):
    "Add `ParallelTrainer` callback to a `Learner`"
    self.add_cb(ParallelTrainer(device_ids))
    return self

# Cell
@patch
def detach_parallel(self: Learner):
    "Remove `ParallelTrainer` callback from a Learner"
    self.remove_cb(ParallelTrainer)
    return self

# Cell
@patch
@contextmanager
def parallel_ctx(self: Learner, device_ids=None):
    "A context manager to adapt a learner to train in data parallel mode."
    try:
        self.to_parallel(device_ids)
        yield self
    finally: self.detach_parallel()

# Cell
@patch
def reset(self: DistributedDataParallel):
    "Patch required `reset` call into `DistributedDataParallel`"
    if hasattr(self.module, 'reset'): self.module.reset()

# Cell
def setup_distrib(gpu=None):
    "Setup this process to participate in distributed training"
    if gpu is None: return gpu
    gpu = int(gpu)
    torch.cuda.set_device(int(gpu))
    if num_distrib() > 0: torch.distributed.init_process_group(backend='nccl', init_method='env://')
    return gpu

# Cell
def teardown_distrib():
    "Free distributed training resources"
    if torch.distributed.is_initialized(): torch.distributed.destroy_process_group()

# Cell
def _round_to_multiple(number,multiple): return int(math.ceil(number/multiple)*multiple)

# Cell
class DistributedDL(TfmdDL):
    "A `TfmdDL` which splits a batch into equal size pieces for each worker"
    def __init__(self,dl,rank=None,world_size=None):
        if rank is None: rank=rank_distrib()
        if world_size is None: world_size=num_distrib()
        store_attr()
        if type(dl) == torch.utils.data.DataLoader:
            shuffle = True if eq(type(dl.sampler), torch.utils.data.RandomSampler) else False
            self.dl = DataLoader(dataset=dl.dataset, bs=dl.batch_size, num_workers=dl.num_workers, \
                pin_memory=dl.pin_memory, timeout=dl.timeout, shuffle=shuffle, drop_last=dl.drop_last, persistent_workers=dl.persistent_workers)
        self.bs,self.device,self.drop_last,self.dataset,fake,self.num_workers,self.offs,self.pin_memory = \
            attrgetter('bs','device','drop_last','dataset','fake_l','num_workers','offs','pin_memory')(self.dl)
        self.fake_l = _FakeLoader(self, fake.pin_memory, fake.num_workers, fake.timeout,
                                  persistent_workers=fake.persistent_workers,
                                  pin_memory_device=fake.pin_memory_device)

    def _broadcast(self,t,rank):
        "Broadcasts t from rank `rank` to all other ranks. Returns t so t is same for all ranks after call."
        t = LongTensor(t).cuda() # nccl only works with cuda tensors
        torch.distributed.broadcast(t,rank)
        return t.cpu().tolist()

    def _to_detach(self,b,cpu=True,gather=True): return to_detach(b,cpu,gather) # member func so we can override for test
    def __len__(self): return _round_to_multiple(len(self.dl),self.world_size)//self.world_size
    def get_idxs(self):
        idxs = list(self.dl.get_idxs()) # compute get_idxs in all ranks (we'll only use rank 0 but size must be consistent)
        idxs = self._broadcast(idxs,0)  # broadcast and receive it from rank 0 to all
        self.n = len(idxs)              # we assumed n was dl.n but we really care about number of idxs
        # add extra samples to make it evenly divisible
        self.n_padded = _round_to_multiple(self.n,self.world_size)
        idxs += (idxs * (self.n_padded//self.n))[:self.n_padded-self.n] # idx needs to be repeated when n_padded>>n
        # slice padded idxs so that each rank gets self.n_padded//self.world_size tensors
        return idxs[self.rank*self.n_padded//self.world_size:(self.rank+1)*self.n_padded//self.world_size]

    def before_iter(self):
        self.i = 0
        self.dl.before_iter()

    def randomize(self): self.dl.randomize()
    def after_batch(self,b):
        self.i += find_bs(b)
        return self.dl.after_batch(b)

    def after_iter(self):  self.dl.after_iter()
    def create_batches(self,samps): return self.dl.create_batches(samps)
    def to_detach(self,b, cpu=True, gather=True):
        b = self._to_detach(b, cpu, gather)
        def _inner(b):
            if b.ndim>0:
                # for each rank, compute overflow of read idxs vs self.n and accumulate them to unpad totals after gathering
                n = sum([min(0,max(-len(b)//self.world_size,
                                   self.n-(self.i+r*self.n_padded//self.world_size))) for r in range(self.world_size)])
                b = b[:n or None]
            return b
        return apply(_inner,b) if gather and all(hasattr(self,o) for o in ('i','n','n_padded')) else b

# Cell
_hidden_params = ["mixed_precision", "fp16", "log_with", "logging_dir", "step_scheduler_with_optimizer"]

# Cell
class DistributedTrainer(Callback):
    "Wrap `model` in `DistributedDataParallel` and `dls` in `DistributedDL`"
    order = 11
    @delegates(Accelerator, but=_hidden_params)
    def __init__(self,
        sync_bn=True, # Whether to replace all batch norm with `nn.SyncBatchNorm`
        **kwargs
    ):
        store_attr()
        self.accelerator = Accelerator(**kwargs)
    def before_fit(self):
        self.learn.model = self.accelerator.prepare(
            nn.SyncBatchNorm.convert_sync_batchnorm(self.model) if self.sync_bn else self.model
        )
        self.old_dls = list(self.dls)
        self.learn.dls.loaders = [self._wrap_dl(dl) for dl in self.dls]
        if rank_distrib(): self.learn.logger=noop

    def _wrap_dl(self, dl): return dl if isinstance(dl,DistributedDL) else DistributedDL(dl)
    def _backward(self): self.accelerator.backward(self.learn.loss_grad)

    def before_train(self):    self.learn.dl = self._wrap_dl(self.learn.dl)
    def before_validate(self): self.learn.dl = self._wrap_dl(self.learn.dl)
    def after_fit(self): self.learn.model,self.learn.dls.loaders = self.learn.model.module,self.old_dls

# Cell
@patch
@delegates(Accelerator, but=_hidden_params)
def to_distributed(self: Learner,
        sync_bn=True, # Whether to replace all batch norm with `nn.SyncBatchNorm`
        **kwargs
    ):
    "Add `AcceleratedTrainer` to a learner, and configures an Accelerator"
    self.add_cb(DistributedTrainer(sync_bn, **kwargs))
    if rank_distrib(): self.remove_cb(ProgressCallback)
    return self

# Cell
@patch
def detach_distributed(self: Learner):
    "Remove `DistributedTrainer` from a learner"
    if num_distrib() <=1: return self
    self.remove_cb(DistributedTrainer)
    if rank_distrib() and not hasattr(self, 'progress'): self.add_cb(ProgressCallback())
    return self

# Cell
@patch
@contextmanager
@delegates(Accelerator, but=_hidden_params)
def distrib_ctx(self: Learner,
        sync_bn=True, # Whether to replace all batch norm with `nn.SyncBatchNorm`
        in_notebook=False, # Whether we are launching from a notebook or not
        **kwargs
   ):
    "A context manager to adapt a learner to train in distributed data parallel mode."
    try: import accelerate
    except ImportError as e:
        e.args = ["Accelerate is required. Install with `pip install accelerate`"]
        raise
    # Adapt self to DistributedDataParallel, yield, and cleanup afterwards.
    cleanup_dpg = False
    try:
        if in_notebook:
            cuda_id = rank_distrib()
            if not torch.distributed.is_initialized():
                setup_distrib(cuda_id)
                cleanup_dpg = torch.distributed.is_initialized()
            if not rank_distrib(): print("Training Learner...")
        if num_distrib(): self.to_distributed(sync_bn, **kwargs)
        yield self
    finally:
        self.detach_distributed()
        if cleanup_dpg: teardown_distrib()

# Cell
def rank0_first(func, *args, **kwargs):
    "Execute `func` in the Rank-0 process first, then in other ranks in parallel."
    if args or kwargs: func = partial(func, *args, **kwargs)
    dummy_l = Learner(DataLoaders(device='cpu'), nn.Linear(1,1), loss_func=lambda: 0)
    with dummy_l.distrib_ctx():
        if not rank_distrib(): res = func()
        distrib_barrier()
        if rank_distrib(): res = func()
    return res