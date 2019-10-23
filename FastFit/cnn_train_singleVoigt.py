import os
import pdb
import time
import json
import numpy as np
from pyigm.fN.fnmodel import FNModel
from pyigm.fN.mockforest import monte_HIcomp
from scipy.special import wofz
from utilities import generate_wave, rebin_subpix
from matplotlib import pyplot as plt

import tensorflow as tf
import keras.backend as K
from keras.utils import plot_model, multi_gpu_model
from keras.callbacks import ModelCheckpoint, CSVLogger
from keras.models import Model, load_model
from keras.layers import Input
from keras.layers import Dense
from keras.layers import Flatten
from keras.layers.convolutional import Conv1D
from keras.layers.convolutional import MaxPooling1D
from keras.layers.merge import concatenate
from keras.layers import Dropout, BatchNormalization
from contextlib import redirect_stdout

velstep = 2.5    # Pixel size in km/s
spec_len = 256
spec_ext = 64


# Define custom loss
def mse_mask():
    # Create a loss function that adds the MSE loss to the mean of all squared activations of a specific layer
    def loss(y_true, y_pred):
        epsilon = K.ones_like(y_true[0,:])*0.00001
        return K.mean( (y_true/(y_true+epsilon)) * K.square(y_pred - y_true), axis=-1)
        #return K.mean(K.square(y_pred - y_true), axis=-1)
    # Return a function
    return loss


def hyperparam(mnum):
    """Generate a random set of hyper parameters

    mnum (int): Model index number
    """
    # Define all of the allowed parameter space
    allowed_hpars = dict(learning_rate       = [0.00002, 0.0005, 0.0007, 0.0010, 0.0030, 0.0050, 0.0070],
                         batch_size          = [],
                         l2_regpen           = [0.005, 0.01, 0.008, 0.005, 0.003],
                         dropout_keep_prob   = [0.75, 0.9, 0.95, 0.98, 1],
                         num_epochs          = [3, 5, 7, 9],
                         # Number of filters in each convolutional layer
                         conv_layer_filter_1 = [64, 80, 90, 100, 110, 120, 140, 160, 200],
                         conv_layer_filter_2 = [80, 96, 128, 192, 256],
                         conv_layer_filter_3 = [80, 96, 128, 192, 256],
                         # Kernal size
                         conv_layer_kernal_1 = [20, 22, 24, 26, 28, 32, 40, 48, 54],
                         conv_layer_kernal_2 = [10, 14, 16, 20, 24, 28, 32, 34],
                         conv_layer_kernal_3 = [10, 14, 16, 20, 24, 28, 32, 34],
                         # Stride of each kernal
                         conv_layer_stride_1 = [1, 2, 4, 6],
                         conv_layer_stride_2 = [1, 2, 4, 6],
                         conv_layer_stride_3 = [1, 2, 4, 6],
                         # Pooling kernal size
                         pool_layer_kernal_1 = [2, 3, 4, 6, 8],
                         pool_layer_kernal_2 = [2, 3, 4, 6, 8],
                         pool_layer_kernal_3 = [2, 4, 4, 6, 8],
                         # Pooling stride
                         pool_layer_stride_1 = [1, 2, 4, 5, 6],
                         pool_layer_stride_2 = [1, 2, 3, 4, 5, 6, 7, 8],
                         pool_layer_stride_3 = [1, 2, 3, 4, 5, 6, 7, 8],
                         # Fully connected layers
                         fc1_neurons         = [256, 512, 1024, 2048, 4096],
                         fc2_N_neurons=[32, 64, 128, 256],
                         fc2_z_neurons=[32, 64, 128, 256],
                         fc2_b_neurons=[32, 64, 128, 256],
                         )
    # Generate dictionary of values
    hyperpar = dict({})
    for key in allowed_hpars.keys():
        hyperpar[key] = np.random.choice(allowed_hpars[key])
    # Save these parameters and return the hyperpar
    with open('fit_data/simple/model_{0:03d}.json'.format(mnum), 'w') as fp:
        json.dump(hyperpar, fp, sort_keys=True, indent=4)
    return hyperpar


def voigt(wave, params):
    p0, p1, p2 = params
    lam, fvl, gam = 1215.6701, 0.4164, 6.265E8
    cold = 10.0**p0
    zp1=p1+1.0
    wv=lam*1.0e-8
    bl=p2*wv/2.99792458E5
    a=gam*wv*wv/(3.76730313461770655E11*bl)
    cns=wv*wv*fvl/(bl*2.002134602291006E12)
    cne=cold*cns
    ww=(wave*1.0e-8)/zp1
    v=wv*ww*((1.0/ww)-(1.0/wv))/bl
    tau = cne*wofz(v + 1j * a).real
    return np.exp(-1.0*tau)


def make_spectra(NHI, zabs, bval, nsubpix=10, szstr=None, numspec=1):
    if szstr is None:
        szstr = spec_len+spec_ext
    nspec = NHI.size
    vprofs = np.zeros((szstr, NHI.size))
    for ss in range(nspec):
        if ss%10000 == 0:
            print(ss, nspec)
        lam = 1215.6701*(1+zabs[ss])
        wmin = lam * (1 - 200*velstep/299792.458)
        wmax = lam * (1 + 200*velstep/299792.458)
        wave, subwave = generate_wave(wavemin=wmin, wavemax=wmax, velstep=velstep, nsubpix=nsubpix)
        amn = np.argmin(np.abs(subwave-lam))
        flux = voigt(subwave[amn-nsubpix*szstr//2: amn+nsubpix*szstr//2], [NHI[ss], zabs[ss], bval[ss]])
        vprofs[:, ss] = rebin_subpix(flux, nsubpix=nsubpix)
    suffix = "zem3_nsubpix{0:d}_numspec{1:d}".format(nsubpix, numspec)
    np.save("train_data/svoigt_prof_{0:s}".format(suffix), vprofs)
    np.save("train_data/svoigt_Nval_{0:s}".format(suffix), NHI)
    np.save("train_data/svoigt_bval_{0:s}".format(suffix), bval/velstep)
    return


def generate_dataset(zem=3.0, snr=0, seed=1234, ftrain=0.9, nsubpix=10, numspec=1000, epochs=10):
    # Setup params
    rstate = np.random.RandomState(seed)
    zmin = 1026.0*(1+zem)/1215.6701 - 1

    # Get the CDDF
    NHIp = np.array([12.0, 15.0, 17.0, 18.0, 20.0, 21.0, 21.5, 22.0])
    sply = np.array([-9.72, -14.41, -17.94, -19.39, -21.28, -22.82, -23.95, -25.50])
    params = dict(sply=sply)
    NHI, zabs, bval = np.array([]), np.array([]), np.array([])
    for ss in range(numspec):
        fN_model = FNModel('Hspline', pivots=NHIp, param=params, zmnx=(2., 5.))
        HI_comps = monte_HIcomp((zmin, zem), fN_model, NHI_mnx=(12., 18.), rstate=rstate)
        # Extract/store information
        NHI = np.append(NHI, HI_comps['lgNHI'].data)
        zabs = np.append(zabs, HI_comps['z'].data)
        bval = np.append(bval, HI_comps['bval'].value)
    make_spectra(NHI, zabs, bval, nsubpix=nsubpix, numspec=numspec)


def load_dataset(zem=3.0, snr=0, ftrain=0.9, epochs=10):
    zstr = "zem{0:.2f}".format(zem)
    fdata = np.load("train_data/svoigt_prof_zem3_nsubpix10_numspec1000.npy").T
    Nlabel = np.load("train_data/svoigt_Nval_zem3_nsubpix10_numspec1000.npy")
    blabel = np.load("train_data/svoigt_bval_zem3_nsubpix10_numspec1000.npy")
    zlabel = ((spec_len+spec_ext)//2)*np.ones(Nlabel.size)
    ntrain = int(ftrain*fdata.shape[0])
    # Select the training data
    trainX = fdata[:ntrain, :]
    trainN = Nlabel[:ntrain]
    trainb = blabel[:ntrain]
    # Select the test data
    testX = fdata[ntrain:, :]
    testN = Nlabel[ntrain:]
    testb = blabel[ntrain:]
    return trainX, trainN, trainb, testX, testN, testb


def yield_data(data, Nlabels, blabels, maskval=0.0):
    cntr_batch, batch_sz = 0, 10000
    cenpix = (spec_len+spec_ext)//2
    ll = np.arange(batch_sz).repeat(spec_len)
    while True:
        indict = ({})
        pertrb = np.random.randint(0, spec_ext, batch_sz)
        pp = pertrb.reshape((batch_sz, 1)).repeat(spec_len, axis=1) + np.arange(spec_len)
        X_batch = data[ll+cntr_batch, pp.flatten()].reshape((batch_sz, spec_len))
        indict['input_1'] = X_batch.copy().reshape((batch_sz, spec_len, 1))
        z_batch = spec_len//2 - cenpix + pertrb.copy()
        # Extract the relevant bits of information
        yld_N = Nlabels[cntr_batch:cntr_batch+batch_sz]
        yld_z = z_batch
        yld_b = blabels[cntr_batch:cntr_batch+batch_sz]
        # Mask
        if True:
            wmsk = np.where(X_batch[:, spec_len//2] > 0.95)
            yld_N[wmsk] = maskval
            yld_z[wmsk] = maskval  # Note, this will mask true zeros in the yld_z array
            yld_b[wmsk] = maskval
        # Store output
        outdict = {'output_N': yld_N,
                   'output_z': yld_z,
                   'output_b': yld_b}

#        pdb.set_trace()
        yield (indict, outdict)

        cntr_batch += batch_sz
        if cntr_batch >= data.shape[0]-batch_sz:
            cntr_batch = 0


def build_model_simple(hyperpar):
    # Extract parameters
    learning_rate = hyperpar['learning_rate']
    batch_size = hyperpar['batch_size']
    l2_regpen = hyperpar['l2_regpen']
    fc1_neurons = hyperpar['fc1_neurons']
    fc2_N_neurons = hyperpar['fc2_N_neurons']
    fc2_b_neurons = hyperpar['fc2_b_neurons']
    fc2_z_neurons = hyperpar['fc2_z_neurons']
    conv1_kernel = hyperpar['conv1_kernel']
    conv2_kernel = hyperpar['conv2_kernel']
    conv3_kernel = hyperpar['conv3_kernel']
    conv1_filter = hyperpar['conv1_filter']
    conv2_filter = hyperpar['conv2_filter']
    conv3_filter = hyperpar['conv3_filter']
    conv1_stride = hyperpar['conv1_stride']
    conv2_stride = hyperpar['conv2_stride']
    conv3_stride = hyperpar['conv3_stride']
    pool1_kernel = hyperpar['pool1_kernel']
    pool2_kernel = hyperpar['pool2_kernel']
    pool3_kernel = hyperpar['pool3_kernel']
    pool1_stride = hyperpar['pool1_stride']
    pool2_stride = hyperpar['pool2_stride']
    pool3_stride = hyperpar['pool3_stride']

    # Build model
    inputs = []
    # Shape is (batches, steps, channels)
    # For example, a 3-color 1D image of side 100 pixels, dealt in batches of 32 would have a shape=(32,100,3)
    inputs.append(Input(shape=(spec_len, 1), name='input_1'))
    conv1 = Conv1D(filters=conv1_filter, kernel_size=conv1_kernel, strides=conv1_stride, activation='relu')(inputs[-1])
    pool1 = MaxPooling1D(pool_size=pool1_kernel, strides=pool1_stride)(conv1)
    conv2 = Conv1D(filters=conv2_filter, kernel_size=conv2_kernel, strides=conv2_stride, activation='relu')(pool1)
    pool2 = MaxPooling1D(pool_size=pool2_kernel, strides=pool2_stride)(conv2)
    conv3 = Conv1D(filters=conv3_filter, kernel_size=conv3_kernel, strides=conv3_stride, activation='relu')(pool2)
    pool3 = MaxPooling1D(pool_size=pool3_kernel, strides=pool3_stride)(conv3)
    # Interpretation model
    fullcon1 = Dense(fc1_neurons, activation='relu')(pool3)
    fullcon2_N = Dense(fc2_N_neurons, activation='relu')(fullcon1)
    fullcon2_z = Dense(fc2_z_neurons, activation='relu')(fullcon1)
    fullcon2_b = Dense(fc2_b_neurons, activation='relu')(fullcon1)
    output_N = Dense(1, activation='linear', name='output_N')(fullcon2_N)
    output_z = Dense(1, activation='linear', name='output_z')(fullcon2_z)
    output_b = Dense(1, activation='linear', name='output_b')(fullcon2_b)
    model = Model(inputs=inputs, outputs=[output_N, output_z, output_b])
    return model


# fit and evaluate a model
def evaluate_model(trainX, trainN, trainb,
                   testX, testN, testb, hyperpar,
                   epochs=10, verbose=1):
    #yield_data(trainX, trainN, trainb)
    #assert(False)
    filepath = os.path.dirname(os.path.abspath(__file__))
    model_name = "/fit_data/svoigt_speclen{0:d}_masked".format(spec_len)
    inputs = []
    concat_arr = []
    kernsz = [32]
    # Construct network
    model = build_model_simple(hyperpar)
    # Make this work on multiple GPUs
    gpumodel = multi_gpu_model(model, gpus=4)
    # Summarize layers
    with open(filepath + model_name + '.summary', 'w') as f:
        with redirect_stdout(f):
            gpumodel.summary()
    # Plot graph
    pngname = filepath + model_name + '.png'
    plot_model(gpumodel, to_file=pngname)
    # Compile
    masking = True
    if masking:
        loss = {'N_output': mse_mask(),
                'z_output': mse_mask(),
                'b_output': mse_mask()}
    else:
        loss = {'N_output': 'mse',
                'z_output': 'mse',
                'b_output': 'mse'}
    gpumodel.compile(loss=loss, optimizer='adam', metrics=['mean_squared_error'])
    # Initialise callbacks
    ckp_name = filepath + model_name + '.hdf5'
    sav_name = filepath + model_name + '_save.hdf5'
    csv_name = filepath + model_name + '.log'
    checkpointer = ModelCheckpoint(filepath=ckp_name, verbose=1, save_best_only=True)
    csv_logger = CSVLogger(csv_name, append=True)
    # Fit network
    gpumodel.fit_generator(
        yield_data(trainX, trainN, trainb),
        steps_per_epoch=2000,  # Total number of batches (i.e. num data/batch size)
        epochs=epochs, verbose=verbose,
        callbacks=[checkpointer, csv_logger],
        validation_data=yield_data(testX, testN, testb),
        validation_steps=200)

    gpumodel.save(sav_name)

    # Evaluate model
#    _, accuracy
    accuracy = gpumodel.evaluate_generator(yield_data(testX, testN, testb),
                                           steps=testX.shape[0],
                                           verbose=0)
    return accuracy, gpumodel.metrics_names


def restart_model(model_name, trainX, trainN, trainb,
                   testX, testN, testb,
                   epochs=10, verbose=1, masking=True):
    filepath = os.path.dirname(os.path.abspath(__file__))+'/'
    # Load model
    loadname = filepath + 'fit_data/' + model_name + '.hdf5'
    model = load_model(loadname, compile=False)
    # Make this work on multiple GPUs
    gpumodel = multi_gpu_model(model, gpus=4)
    # Compile model
    if masking:
        loss = {'N_output': mse_mask(),
                'z_output': mse_mask(),
                'b_output': mse_mask()}
    else:
        loss = {'N_output': 'mse',
                'z_output': 'mse',
                'b_output': 'mse'}
    gpumodel.compile(loss=loss, optimizer='adam', metrics=['mean_squared_error'])
    # Initialise callbacks
    ckp_name = filepath + model_name + '_chkp_restart.hdf5'
    sav_name = filepath + model_name + '_save_restart.hdf5'
    csv_name = filepath + model_name + '_restart.log'
    checkpointer = ModelCheckpoint(filepath=ckp_name, verbose=1, save_best_only=True)
    csv_logger = CSVLogger(csv_name, append=True)
    # Fit network
    gpumodel.fit_generator(
        yield_data(trainX, trainN, trainb),
        steps_per_epoch=2000,  # Total number of batches (i.e. num data/batch size)
        epochs=epochs, verbose=verbose,
        callbacks=[checkpointer, csv_logger],
        validation_data=yield_data(testX, testN, testb),
        validation_steps=200)

    gpumodel.save(sav_name)

    # Evaluate model
    #    _, accuracy
    accuracy = gpumodel.evaluate_generator(yield_data(testX, testN, testb),
                                           steps=testX.shape[0],
                                           verbose=0)
    return accuracy, gpumodel.metrics_names


# Gold standard in cross-validation
# from sklearn.model_selection import StratifiedKFold
# seed = 7
# np.random.seed(seed)
# kfold = StratifiedKFold(n_splits=10, shuffle=True, random_state=seed)
# for train, test in kfold.split(X, Y):
#     model.fit(X[train], Y[train], epochs=150, batch_size=10, verbose=0)
#     # evaluate the model
#     scores = model.evaluate(X[test], Y[test], verbose=0)


# summarize scores
def summarize_results(scores):
    keys = scores.keys()
    for ii in keys:
        m, s = np.mean(scores[ii]), np.std(scores[ii])
        print('Accuracy: %.3f%% (+/-%.3f)' % (m, s))


# Detect features in a dataset
def localise_features(repeats=3, epochs=10, restart=False):
    # load data
    trainX, trainN, trainb,\
    testX, testN, testb = load_dataset(epochs=epochs)
    # repeat experiment
    allscores = dict({})
    for r in range(repeats):
        if restart:
            model_name = 'svoigt_speclen256_masked_save'
            scores, names = restart_model(model_name, trainX, trainN, trainb,
                                          testX, testN, testb)
        else:
            scores, names = evaluate_model(trainX, trainN, trainb,
                                           testX, testN, testb, epochs=epochs)
        if r == 0:
            for name in names:
                allscores[name] = []
        for ii, name in enumerate(names):
            allscores[name].append(scores[ii] * 100.0)
            if '_acc' in name:
                print('%s >#%d: %.3f' % (name, r + 1, allscores[name][-1]))
            else:
                print('%s >#%d: %.3f' % (name, r + 1, scores[ii]))
    # Summarize results
    summarize_results(allscores)

# Set the number of epochs
epochs = 10

if False:
    # Generate data
    generate_dataset(epochs=epochs)
else:
    # Once the data exist, run the experiment
    atime = time.time()
    localise_features(repeats=1, epochs=10, restart=True)
    btime = time.time()
    print((btime-atime)/60.0)
