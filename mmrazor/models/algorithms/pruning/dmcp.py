import os
from typing import Dict, List, Optional, Tuple, Union

import random
from typing_extensions import Self
import torch
from mmengine import MMLogger
from mmengine.model import BaseModel, MMDistributedDataParallel
from mmengine.optim import OptimWrapper
from mmengine.structures import BaseDataElement
from torch import nn

from mmrazor.models.distillers import ConfigurableDistiller
from mmrazor.models.mutators.base_mutator import BaseMutator
from mmrazor.models.utils import add_prefix
from mmrazor.registry import MODEL_WRAPPERS, MODELS
from mmrazor.utils import ValidFixMutable
from mmrazor.structures.subnet.fix_subnet import _dynamic_to_static
from ..base import BaseAlgorithm

VALID_MUTATOR_TYPE = Union[BaseMutator, Dict]
VALID_MUTATORS_TYPE = Dict[str, Union[BaseMutator, Dict]]
VALID_DISTILLER_TYPE = Union[ConfigurableDistiller, Dict]

from mmrazor.models.mutators import DMCPChannelMutator
from mmrazor.models.mutators import ChannelMutator
from .ite_prune_algorithm import ItePruneAlgorithm, ItePruneConfigManager

LossResults = Dict[str, torch.Tensor]
TensorResults = Union[Tuple[torch.Tensor], torch.Tensor]
PredictResults = List[BaseDataElement]
ForwardResults = Union[LossResults, TensorResults, PredictResults]

@MODELS.register_module()
class DMCP(ItePruneAlgorithm):

    def __init__(self,
                #  mutators: VALID_MUTATORS_TYPE,
                 distiller: VALID_DISTILLER_TYPE,
                 architecture: Union[BaseModel, Dict],
                 mutator_cfg: Union[Dict, DMCPChannelMutator] = dict(
                     type=' DMCPChannelMutator',
                     channel_unit_cfg=dict(type='DMCPChannelUnit')),
                 fix_subnet: Optional[ValidFixMutable] = None,
                 data_preprocessor: Optional[Union[Dict, nn.Module]] = None,
                 strategy: List = ['max', 'min', 'scheduled_random', 'arch_random'],
                 init_cfg: Optional[Dict] = None,
                 target_pruning_ratio: Optional[Dict[str, float]] = None,
                 arch_start_train=10000, # arch_start_train_iter
                 step_freq=500, # arch_train_freq
                 distillation_times=2000, # distillation_start_train_iter
                 target_flops=150, # MFLOPs
                 flops_loss_type: str = 'log_l1',
                 flop_loss_weight: float = 1.0,
                 linear_schedule=False,
                 is_deployed=False) -> None:
        super().__init__(architecture, mutator_cfg, data_preprocessor,
                         target_pruning_ratio, step_freq,
                         init_cfg, linear_schedule)
        
        self.arch_start_train = arch_start_train
        self.strategy = strategy
        self.distillation_times = distillation_times
        self.target_flops = target_flops

        self.samples = len([s for s in self.strategy if 'random' in s])
        self.is_supernet = True if len(self.strategy) > 1 else False
        self.distiller = self._build_distiller(distiller)
        self.distiller.prepare_from_teacher(self.architecture)
        self.distiller.prepare_from_student(self.architecture)

        self.flops_loss_type = flops_loss_type
        self.flop_loss_weight = flop_loss_weight
        self.cur_sample_prob = 1.0
        self.arch_train = False

        if fix_subnet:
            # Avoid circular import
            from mmrazor.structures import load_fix_subnet

            # According to fix_subnet, delete the unchosen part of supernet
            load_fix_subnet(self.architecture, fix_subnet)
            self.is_supernet = False
        self.is_deployed = is_deployed
        if (self.is_deployed):
            # To static ops for loaded pruned network.
            self._deploy()
    
    def _deploy(self):
        config = self.prune_config_manager.prune_at(self._iter)
        self.mutator.set_choices(config)
        self.mutator.fix_channel_mutables()
        self._fix_archtecture()
        _dynamic_to_static(self.architecture)
        self.is_deployed = True

    def _build_mutator(self, mutator: VALID_MUTATOR_TYPE) -> BaseMutator:
        """build mutator."""
        if isinstance(mutator, dict):
            mutator = MODELS.build(mutator)
        if not isinstance(mutator, BaseMutator):
            raise TypeError('mutator should be a `dict` or '
                            '`OneShotModuleMutator` instance, but got '
                            f'{type(mutator)}')

        return mutator

    def _build_distiller(
            self, distiller: VALID_DISTILLER_TYPE) -> ConfigurableDistiller:
        if isinstance(distiller, dict):
            distiller = MODELS.build(distiller)
        if not isinstance(distiller, ConfigurableDistiller):
            raise TypeError('distiller should be a `dict` or '
                            '`ConfigurableDistiller` instance, but got '
                            f'{type(distiller)}')

        return distiller

    def set_subnet(self, mode, arch_train=None) -> None:
        """Set subnet by 'max' 'min' 'random' 'direct' or 'expected."""
        assert mode in ('max', 'min', 'random', 'direct', 'expected')
        if arch_train is None:
            arch_train = self.arch_train
        self.mutator.sample_subnet(mode, arch_train)

    def train_step(self, data: List[dict],
                   optim_wrapper: OptimWrapper) -> Dict[str, torch.Tensor]:

        if not self.arch_train and \
            self._iter > self.arch_start_train:
            self.arch_train = True

        if self.is_supernet:
            def distill_step(
                batch_inputs: torch.Tensor, data_samples: List[BaseDataElement]
            ) -> Dict[str, torch.Tensor]:
                subnet_losses = dict()
                with optim_wrapper['architecture'].optim_context(
                        self
                ), self.distiller.student_recorders:  # type: ignore
                    hard_loss = self(batch_inputs, data_samples, mode='loss')
                    soft_loss = self.distiller.compute_distill_losses()

                    subnet_losses.update(hard_loss)
                    if self._iter > self.distillation_times:
                        subnet_losses.update(soft_loss)

                    parsed_subnet_losses, _ = self.parse_losses(subnet_losses)
                    optim_wrapper['architecture'].update_params(parsed_subnet_losses)

                return subnet_losses

            batch_inputs, data_samples = self.data_preprocessor(
                data, True).values()
            total_losses = dict()

            #update model parameters
            for kind in self.strategy:
                if kind in ('max'): 
                    self.set_subnet(mode='max')
                    with optim_wrapper['architecture'].optim_context(
                            self
                    ), self.distiller.teacher_recorders:  # type: ignore
                        max_subnet_losses = self(
                            batch_inputs, data_samples, mode='loss')
                        parsed_max_subnet_losses, _ = self.parse_losses(
                            max_subnet_losses)
                        optim_wrapper['architecture'].update_params(parsed_max_subnet_losses)
                    total_losses.update(add_prefix(max_subnet_losses, 'max_subnet'))
                elif kind in ('min'):
                    self.set_subnet(mode='min')
                    min_subnet_losses = distill_step(batch_inputs, data_samples)
                    total_losses.update(add_prefix(min_subnet_losses, 'min_subnet'))
                elif kind in ('arch_random'):
                    if self.arch_train:
                        self.set_subnet(mode='direct')
                        direct_subnet_losses = distill_step(
                            batch_inputs, data_samples)
                        total_losses.update(
                            add_prefix(direct_subnet_losses, 
                                       'direct_subnet'))
                    else:
                        self.set_subnet(mode='random') 
                        random_subnet_losses = distill_step(
                            batch_inputs, data_samples)
                        total_losses.update(
                            add_prefix(random_subnet_losses,
                                       'random_subnet'))
                elif kind in ('scheduled_random'):
                    if random.uniform(0, 1) > self.cur_sample_prob\
                       and self.arch_train:
                        self.set_subnet(mode='direct')
                        direct_subnet_losses = distill_step(
                            batch_inputs, data_samples)
                        total_losses.update(
                            add_prefix(direct_subnet_losses, 
                                       'direct_subnet'))
                    else:
                        self.set_subnet(mode='random') 
                        random_subnet_losses = distill_step(
                            batch_inputs, data_samples)
                        total_losses.update(
                            add_prefix(random_subnet_losses,
                                       'random_subnet'))
                    self.cur_sample_prob *= 0.9999

            #update arch parameters
            if self.arch_train \
                and self._iter % self.step_freq == 0:
                with optim_wrapper['mutator'].optim_context(self):
                    optim_wrapper['mutator'].zero_grad()
                    mutator_loss = self._update_arch_params(
                        batch_inputs, data_samples, optim_wrapper, mode='loss')
                total_losses.update(mutator_loss)
            return total_losses
        else:
            return super().train_step(data, optim_wrapper)

    def _update_arch_params(
                self,
                inputs: torch.Tensor,
                data_samples: Optional[List[BaseDataElement]],
                optim_wrapper: OptimWrapper,
                mode: str = 'loss') -> Dict:
        arch_params_loss = dict()
        self.eval()
        # update arch_loss
        self.set_subnet(mode='max', arch_train=True)
        with optim_wrapper['mutator'].optim_context(self):
            arch_loss = self(inputs, data_samples, mode=mode)
        parsed_arch_loss, _ = self.parse_losses(arch_loss)
        optim_wrapper['mutator'].update_params(parsed_arch_loss)
        arch_params_loss.update(add_prefix(arch_loss, 'arch'))

        # update flops_loss
        self.set_subnet(mode='expected', arch_train=False)
        expected_flops = self.mutator.calc_current_flops(self)
        flops_loss = self._compute_flops_loss(expected_flops).to(
            arch_loss['loss'].device)
        parsed_flops_loss, _ = self.parse_losses({'loss':flops_loss})
        optim_wrapper['mutator'].update_params(parsed_flops_loss)
        arch_params_loss.update(add_prefix({'loss':flops_loss}, 'flops'))
        self.train()
        return arch_params_loss
    
    def _compute_flops_loss(self, expected_flops):
        """Calculation of loss functions of arch parameters.

        Calculate the difference between the expected FLOPs and the
        target FLOPs in the units of M.
        """
        flops_error = expected_flops - self.target_flops

        if self.flops_loss_type == 'l2':
            floss = torch.pow(flops_error, 2)
        elif self.flops_loss_type == 'inverted_log_l1':
            floss = -torch.log(1 / (flops_error + 1e-5))
        elif self.flops_loss_type == 'log_l1':
            if abs(flops_error) > 200:
                ratio = 0.1
            else:
                ratio = 1.0
            # piecewise log function
            lower_flops = self.target_flops * 0.95
            if expected_flops < lower_flops:
                floss = torch.log(ratio * abs(flops_error))
            elif (lower_flops <= expected_flops < self.target_flops):
                floss = expected_flops * 0
            else:
                floss = (
                    torch.log(ratio * abs(expected_flops - (lower_flops))))
        elif self.flops_loss_type == 'l1':
            floss = abs(flops_error)
        else:
            raise NotImplementedError
        return floss * self.flop_loss_weight


    def forward(self,
                inputs: torch.Tensor,
                data_samples: Optional[List[BaseDataElement]] = None,
                mode: str = 'loss') -> ForwardResults:
        """Forward."""
        return BaseAlgorithm.forward(self, inputs, data_samples, mode)

@MODEL_WRAPPERS.register_module()
class DMCPDDP(MMDistributedDataParallel):

    def __init__(self,
                 *,
                 device_ids: Optional[Union[List, int, torch.device]] = None,
                 **kwargs) -> None:
        if device_ids is None:
            if os.environ.get('LOCAL_RANK') is not None:
                device_ids = [int(os.environ['LOCAL_RANK'])]
        super().__init__(device_ids=device_ids, **kwargs)

    def train_step(self, data: List[dict],
                   optim_wrapper: OptimWrapper) -> Dict[str, torch.Tensor]:

        if not self.module.arch_train and \
            self.module._iter > self.module.arch_start_train:
            self.module.arch_train = True

        if self.module.is_supernet:
            def distill_step(
                batch_inputs: torch.Tensor, data_samples: List[BaseDataElement]
            ) -> Dict[str, torch.Tensor]:
                subnet_losses = dict()
                with optim_wrapper['architecture'].optim_context(
                        self
                ), self.module.distiller.student_recorders:  # type: ignore
                    hard_loss = self(batch_inputs, data_samples, mode='loss')
                    soft_loss = self.module.distiller.compute_distill_losses()

                    subnet_losses.update(hard_loss)
                    if self.module._iter > self.module.distillation_times:
                        subnet_losses.update(soft_loss)

                    parsed_subnet_losses, _ = self.module.parse_losses(subnet_losses)
                    optim_wrapper['architecture'].update_params(parsed_subnet_losses)

                return subnet_losses

            batch_inputs, data_samples = self.module.data_preprocessor(
                data, True).values()
            total_losses = dict()
            #update model parameters
            max_net_num = min_net_num = random_net_num = direct_net_num = 1
            for kind in self.module.strategy:
                if kind in ('max'): 
                    self.module.set_subnet(mode='max')
                    with optim_wrapper['architecture'].optim_context(
                            self
                    ), self.module.distiller.teacher_recorders:  # type: ignore
                        max_subnet_losses = self(
                            batch_inputs, data_samples, mode='loss')
                        parsed_max_subnet_losses, _ = self.module.parse_losses(
                            max_subnet_losses)
                        optim_wrapper['architecture'].update_params(parsed_max_subnet_losses)
                    total_losses.update(add_prefix(max_subnet_losses,
                                                   f'max_subnet{max_net_num}'))
                    max_net_num += 1
                elif kind in ('min'):
                    self.module.set_subnet(mode='min')
                    min_subnet_losses = distill_step(batch_inputs, data_samples)
                    total_losses.update(add_prefix(min_subnet_losses,
                                                   f'min_subnet{min_net_num}'))
                    min_net_num += 1
                elif kind in ('arch_random'):
                    if self.module.arch_train:
                        self.module.set_subnet(mode='direct')
                        direct_subnet_losses = distill_step(
                            batch_inputs, data_samples)
                        total_losses.update(
                            add_prefix(direct_subnet_losses, 
                                       f'direct_subnet{direct_net_num}'))
                        direct_net_num += 1
                    else:
                        self.module.set_subnet(mode='random') 
                        random_subnet_losses = distill_step(
                            batch_inputs, data_samples)
                        total_losses.update(
                            add_prefix(random_subnet_losses,
                                       f'random_subnet{random_net_num}'))
                        random_net_num += 1
                elif kind in ('scheduled_random'):
                    if random.uniform(0, 1) > self.module.cur_sample_prob\
                       and self.module.arch_train:
                        self.module.set_subnet(mode='direct')
                        direct_subnet_losses = distill_step(
                            batch_inputs, data_samples)
                        total_losses.update(
                            add_prefix(direct_subnet_losses, 
                                       f'direct_subnet{direct_net_num}'))
                        direct_net_num += 1
                    else:
                        self.module.set_subnet(mode='random') 
                        random_subnet_losses = distill_step(
                            batch_inputs, data_samples)
                        total_losses.update(
                            add_prefix(random_subnet_losses,
                                       f'random_subnet{random_net_num}'))
                        random_net_num += 1
                    self.module.cur_sample_prob *= 0.9999

            with optim_wrapper['mutator'].optim_context(self):
                    optim_wrapper['mutator'].zero_grad()
                    mutator_loss = self.module._update_arch_params(
                        batch_inputs, data_samples, optim_wrapper, mode='loss')
            total_losses.update(mutator_loss)

            # update arch parameters
            if self.module.arch_train \
                and self.module._iter % self.modqule.step_freq == 0:
                with optim_wrapper['mutator'].optim_context(self):
                    optim_wrapper['mutator'].zero_grad()
                    mutator_loss = self.module._update_arch_params(
                        batch_inputs, data_samples, optim_wrapper, mode='loss')
                total_losses.update(mutator_loss)
            return total_losses
        else:
            return super().train_step(data, optim_wrapper)
