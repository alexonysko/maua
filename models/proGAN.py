import time, timeit, datetime, os, math, copy
from itertools import accumulate
import numpy as np
import torch as th
from torch.nn import AvgPool2d, DataParallel
from torch.optim import Adam
import torchvision as tv
from torchvision.utils import save_image
import torchvision.transforms as tn
from torch.nn.functional import interpolate
from .base import BaseModel
from ..dataloaders.proGAN import ProGANDataLoader
from ..networks.generators import ProGrowGenerator as Generator
from ..networks.discriminators import ProGrowDiscriminator as Discriminator
from ..networks.losses import *


class ProGAN(BaseModel):
    """ Wrapper around the Generator and the Discriminator """

    def __init__(self, depth=7, latent_size=256, num_channels=3, learning_rate=1e-3, beta_1=0,
                 beta_2=0.99, eps=1e-8, drift=0.001, use_eql=True, use_ema=True, ema_decay=0.999,
                 checkpoint=None, **kwargs):
        """
        constructor for the class ProGAN, extends BaseModel
        :param depth: depth of the GAN, 2^depth is the final size of generated images
        :param latent_size: latent size of the manifold used by the GAN
        :param num_channels: *NOT YET IMPLEMENTED* will control number of channels of in/outputs
        :param drift: drift penalty for the discriminator
                      (Used only if loss is wgan or wgan-gp)
        :param use_eql: whether to use equalized learning rate
        :param use_ema: boolean for whether to use exponential moving averages
        :param ema_decay: value of mu for ema
        :param checkpoint: generator checkpoint to load for inference
        :param learning_rate: base learning rate for Adam
        :param beta_1: beta_1 parameter for Adam
        :param beta_2: beta_2 parameter for Adam
        :param eps: epsilon parameter for Adam
        """
        super(ProGAN, self).__init__(**kwargs)

        # state of the object
        self.latent_size = latent_size
        self.num_channels = num_channels
        self.depth = depth - 1 # ensures generated images are size 2^depth
        self.use_ema = use_ema
        self.ema_decay = ema_decay
        self.use_eql = use_eql
        self.drift = drift
        self.dataloader = None

        # Create the Generator and the Discriminator
        self.G = Generator(self.depth, self.latent_size, use_eql=self.use_eql).to(self.device)
        self.D = Discriminator(self.depth, self.latent_size, use_eql=self.use_eql).to(self.device)

        # if code is to be run on GPU, we can use DataParallel:
        if self.device == th.device("cuda"):
            self.G = DataParallel(self.G)
            self.D = DataParallel(self.D)

        # define the optimizers for the discriminator and generator
        self.default_rate = learning_rate
        self.G_optim = Adam(self.G.parameters(), lr=learning_rate, betas=(beta_1, beta_2), eps=eps)
        self.D_optim = Adam(self.D.parameters(), lr=learning_rate, betas=(beta_1, beta_2), eps=eps)

        # setup the ema for the generator
        if self.use_ema:
            # create a shadow copy of the generator
            self.G_shadow = copy.deepcopy(self.G)

            # initialize the G_shadow weights equal to the weights of G
            self.update_average(self.G_shadow, self.G, beta=0)

        if checkpoint is not None:
            self.model_names = ['G']
            self.load_networks(checkpoint)
            self.set_requires_grad(self.G, requires_grad=False)


    def setup_loss(self, loss):
        if isinstance(loss, str):
            loss = loss.lower()  # lowercase the string
            if loss == "wgan":
                loss = WGAN_GP(self.device, self.D, self.drift, use_gp=False)
                # note if you use just wgan, you will have to use weight clipping
                # in order to prevent gradient exploding
            elif loss == "wgan-gp":
                loss = WGAN_GP(self.device, self.D, self.drift, use_gp=True)
            elif loss == "lsgan":
                loss = LSGAN(self.D)
            elif loss == "lsgan-sig":
                loss = LSGAN_SIGMOID(self.D)
            elif loss == "hinge":
                loss = HingeLoss(self.D)
            elif loss == "rel-avg":
                loss = RelativisticAverageHinge(self.D)
            elif loss == "r1-reg":
                loss = R1Regularized(self.device, self.D)
            else:
                raise ValueError("Unknown loss function requested")
        elif not isinstance(loss, GANLoss):
            raise ValueError("loss is neither an instance of GANLoss nor a string")
        return loss


    # This function updates the exponential average weights based on the current training
    def update_average(self, model_tgt, model_src, beta):
        """
        update the target model using exponential moving averages
        :param model_tgt: target model
        :param model_src: source model
        :param beta: value of decay beta
        :return: None (updates the target model)
        """
        # turn off gradient calculation
        self.set_requires_grad(model_tgt, False)
        self.set_requires_grad(model_src, False)

        param_dict_src = dict(model_src.named_parameters())

        for p_name, p_tgt in model_tgt.named_parameters():
            p_src = param_dict_src[p_name]
            assert (p_src is not p_tgt)
            p_tgt.copy_(beta * p_tgt + (1. - beta) * p_src)

        # turn back on the gradient calculation
        self.set_requires_grad(model_tgt, True)
        self.set_requires_grad(model_src, True)


    def forward(self, real_A):
        return self.G(real_A, self.depth-1, alpha=1)


    def optimize_D(self, noise, real_batch, depth, alpha):
        self.set_requires_grad(self.G, False)
        self.set_requires_grad(self.D, True)

        # downsample the real_batch for the given depth
        down_sample_factor = int(np.power(2, self.depth - depth - 1)) if not self.dataloader.prescaled_data else 1
        prior_downsample_factor = max(int(np.power(2, self.depth - depth)), 0) if not self.dataloader.prescaled_data else 2

        ds_real_samples = AvgPool2d(down_sample_factor)(real_batch)

        if depth > 0:
            prior_ds_real_samples = interpolate(AvgPool2d(prior_downsample_factor)(real_batch), scale_factor=2)
        else:
            prior_ds_real_samples = ds_real_samples

        # real samples are a combination of ds_real_samples and prior_ds_real_samples
        real_samples = (alpha * ds_real_samples) + ((1 - alpha) * prior_ds_real_samples)

        loss_val = 0
        for _ in range(self.n_critic):
            # optimize discriminator
            self.D_optim.zero_grad()

            # generate a batch of samples
            fake_samples = self.G(noise, depth, alpha).detach()

            loss = self.loss.loss_D(real_samples.requires_grad_(), fake_samples.requires_grad_(), depth=depth, alpha=alpha)

            if not isinstance(self.loss, R1Regularized):
                loss.backward()

            self.D_optim.step()

            loss_val += loss.item()

        return loss_val / self.n_critic


    def optimize_G(self, noise, real_batch, depth, alpha):
        self.set_requires_grad(self.G, True)
        self.set_requires_grad(self.D, False)

        # optimize the generator
        self.G_optim.zero_grad()

        fake_samples = self.G(noise, depth, alpha)

        loss = self.loss.loss_G(real_batch, fake_samples, depth=depth, alpha=alpha)
        loss.backward()

        self.G_optim.step()

        # if use_ema is true, apply ema to the generator parameters
        if self.use_ema:
            self.update_average(self.G_shadow, self.G, self.ema_decay)

        # return the loss value
        return loss.item()


    def train(self, continue_train=False, data_path='maua/datasets/default_progan',
        dataloader=None, start_epoch=1, start_depth=1, until_depth=None, fade_in=0.5, save_freq=25,
        log_freq=5, num_epochs=50, learning_rates_dict={256: 5e-4, 512: 2.5e-4, 1024: 1e-4},
        n_critic=1, loss="wgan-gp"):
        """
        Training function for ProGAN object
        :param continue_train: whether to continue training or not
        :param data_path: path to folder containing images to train on
        :param dataloader: custom dataloader to use, otherwise images will only be resized to max resolution
        :param start_epoch: epoch to continue training from (defaults to most recent, if continuing training)
        :param start_depth: depth to continue training from (defaults to most recent, if continuing training)
        :param until_depth: depth to continue training until (defaults to self.depth)
        :param fade_in: fraction of epochs per depth to fade into the new resolution
        :param save_freq: frequency to save checkpoints in number of epochs
        :param log_freq: frequency to log images in number of or fraction of epochs
        :param learning_rates_dict: dictionary of learning rates per resolution (defaults to self.learning_rate)
        :param n_critic: number of times to update discriminator (Used only if loss is wgan or wgan-gp)
        :param loss: the loss function to be used. Can either be a string =>
                        ["wgan-gp", "wgan", "lsgan", "lsgan-sig", "hinge", "rel-avg", "r1-reg"]
                     or an instance of GANLoss
        """
        self.model_names = ["G", "D"]
        self.n_critic = n_critic
        self.loss = self.setup_loss(loss)

        os.makedirs(os.path.join(self.save_dir, "images"), exist_ok=True)

        start_epoch = epoch = 1
        total_epochs = num_epochs * self.depth
        if continue_train:
            epoch = self.get_latest_network(start_epoch, max_epoch=total_epochs)
            start_depth = start_depth if start_depth != 1 else math.ceil(epoch / num_epochs)
            start_epoch = epoch - math.floor(epoch / num_epochs) * num_epochs

        # create dataloader
        if dataloader is None and self.dataloader is None:
            transforms = tv.transforms.Compose([tn.Resize(2**(self.depth + 1)), tn.ToTensor()])
            dataloader = ProGANDataLoader(data_path=data_path, transforms=transforms)
        dataloader.generate_prescaled_dataset(sizes=list(map(lambda x: 2**(x+3), range(self.depth-1))))
        self.dataloader = dataloader
        batches_dict = self.dataloader.get_batch_sizes(self)
        dataset_size = len(dataloader)
        print('# training images = %d' % dataset_size)

        # create fixed_input for logging
        fixed_input = th.randn(12, self.latent_size).to(self.device)

        print("Starting training on "+str(self.device))
        global_time = time.time()
        for depth in range(start_depth, self.depth if until_depth is None else until_depth):
            current_res = 2**(depth + 2)
            print("Current resolution: %d x %d" % (current_res, current_res))

            # update batch size and learning rate for scale
            dataloader.set_batch_size(current_res, batches_dict[current_res])
            total_batches = dataloader.batches()
            learning_rate = learning_rates_dict.get(current_res, self.default_rate)
            self.D_optim.lr = self.G_optim.lr = learning_rate

            for e in range(start_epoch if depth == start_depth else 1, num_epochs + 1):
                start = time.time()
                
                # calculate the value of alpha for fade-in effect
                alpha = min(e / (num_epochs * fade_in), 1)
                if log_freq < 1: print("Start of epoch: %s / %s \t Fade in: %s"%(epoch, total_epochs, alpha))

                loss_D, loss_G = 0, 0
                for i, batch in enumerate(dataloader, 1):
                    images = batch.to(self.device)
                    noise = th.randn(images.shape[0], self.latent_size).to(self.device)

                    loss_D += self.optimize_D(noise, images, depth, alpha)
                    loss_G += self.optimize_G(noise, images, depth, alpha)

                    if i % math.ceil(total_batches * log_freq) == 0 and not (i == 0 or i == total_batches):
                        elapsed = str(datetime.timedelta(seconds=time.time() - global_time))
                        print("Elapsed: [%s] Batch: %d / %d d_loss: %f  g_loss: %f" %
                                (elapsed, i, total_batches, loss_D / math.ceil(total_batches*log_freq),
                                loss_G / math.ceil(total_batches*log_freq)))
                        loss_D, loss_G = 0, 0

                        # create a grid of samples and save it
                        gen_img_file = os.path.join(self.save_dir, "images", "sample_res%d_e%d_b%d" %
                                                    (current_res, epoch, i) + ".png")
                        with th.no_grad():
                            self.create_grid(
                                samples=self.G(fixed_input, depth, alpha),
                                scale_factor=int(np.power(2, self.depth - depth - 2)),
                                img_file=gen_img_file,
                            )

                if log_freq < 1: print("End of epoch:", epoch, "Took: ", time.time() - start, "sec")

                if log_freq >= 1 and epoch % log_freq == 0 or epoch == total_epochs:
                    elapsed = str(datetime.timedelta(seconds=time.time() - global_time))
                    print("Elapsed: [%s] Epoch: %d / %d Fade in: %.02f d_loss: %f  g_loss: %f" %
                          (elapsed, epoch, num_epochs*(self.depth-1), alpha, loss_D, loss_G))
                    # create a grid of samples and save it
                    gen_img_file = os.path.join(self.save_dir, "images", "sample_res%d_e%d" %
                                                (current_res, epoch) + ".png")
                    with th.no_grad():
                        self.create_grid(
                            samples=self.G(fixed_input, depth, alpha),
                            scale_factor=int(np.power(2, self.depth - depth)/4),
                            img_file=gen_img_file,
                        )

                if epoch % save_freq == 0 or epoch == total_epochs:
                    self.save_networks(epoch)

                epoch += 1

        print("Training finished, took: ", datetime.timedelta(seconds=time.time() - global_time))
        self.save_networks("final")


    # used to create grid of training images for logging
    def create_grid(self, samples, scale_factor, img_file, real_imgs=False):
        samples = th.clamp(samples, min=0, max=1)
        if scale_factor > 1 and not real_imgs:
            samples = interpolate(samples, scale_factor=scale_factor)
        save_image(samples, img_file, nrow=int(np.sqrt(len(samples))+1))

