import os
import argparse
from plantseg.predictions.functional.predictions import unet_predictions
from plantseg.segmentation.functional.segmentation import *
from aicsimageio import AICSImage
from aicsimageio.writers.ome_tiff_writer import OmeTiffWriter
from skimage import exposure
import numpy as np
from tqdm import tqdm
from skimage.measure import regionprops, regionprops_table
from sklearn.model_selection import ParameterGrid
from skimage.segmentation import relabel_sequential
from cellpose import core, utils, io, models, metrics, plot
from cellpose.plot import *
import pyvips
import tifffile
from ome_types.model import OME, Image, Pixels, Channel
from scipy import stats
import multiprocessing
from pyHisto import io, utils, plot

# def pyramidal_ome_tiff_write(image, path, resX=1.0, resY=1.0, units="µm", tile_size=2048, channel_colors=None):
#     """
#     Pyramidal ome tiff write is only support in 2D + C data.
#     Input dimension order has to be XYC
#     """

#     assert len(image.shape) == 3, "Input dimension order must be XYC, get array dimension of {}".format(len(image.shape)) 

#     size_x, size_y, size_c = image.shape
    
#     format_dict = {
#         np.uint8: "uchar",
#         np.uint16: "ushort",
#         np.float32: "float",
#         np.float64: "double"
#     }

#     dtype_dict = {
#         np.uint8: "uint8",
#         np.uint16: "uint16",
#         np.float32: "float",
#         np.float64: "double"
#     }

#     if image.dtype not in list(format_dict.keys()):
#         raise TypeError(f"Expected an uint8/uint16/float32/float64 image, but received {image.dtype}")

#     im_vips = pyvips.Image.new_from_memory(image.transpose(1,0,2).reshape(-1,size_c).tobytes(), size_x, size_y, bands=size_c, format=format_dict[image.dtype.type]) 
#     im_vips = pyvips.Image.arrayjoin(im_vips.bandsplit(), across=1) # for multichannel write
#     im_vips.set_type(pyvips.GValue.gint_type, "page-height", size_y)

#     # build minimal OME metadata
#     ome = OME()

#     if channel_colors is None:
#         channel_colors = [-1 for _ in range(size_c)]

#     img = Image(
#         id="Image:0",
#         name="resolution_1",
#         pixels=Pixels(
#             id="Pixels:0", type=dtype_dict[image.dtype.type], dimension_order="XYZTC",
#             size_c=size_c, size_x=size_x, size_y=size_y, size_z=1, size_t=1, 
#             big_endian=False, metadata_only=True,
#             physical_size_x=resX,
#             physical_size_x_unit=units,
#             physical_size_y=resY,
#             physical_size_y_unit=units,
#             channels= [Channel(id=f"Channel:0:{i}", name=f"Ch_{i}", color=channel_colors[i]) for i in range(size_c)]
#         )
#     )

#     ome.images.append(img)

#     def eval_cb(image, progress):
#         pbar_filesave.update(progress.percent - pbar_filesave.n)

#     im_vips.set_progress(True)

#     pbar_filesave = tqdm(total=100, unit="Percent", desc="Writing pyramidal OME TIFF", position=0, leave=True)
#     im_vips.signal_connect('eval', eval_cb)
#     im_vips.set_type(pyvips.GValue.gstr_type, "image-description", ome.to_xml())

#     im_vips.write_to_file(
#         path, 
#         compression="lzw",
#         tile=True, 
#         tile_width=tile_size,
#         tile_height=tile_size,
#         pyramid=True,
#         depth="onetile",
#         subifd=True,
#         bigtiff=True
#         )

def is_valid_file_or_directory(path):
    """Check if the given path is a valid file or directory."""
    if not os.path.exists(path):
        raise argparse.ArgumentTypeError(f"Path '{path}' does not exist.")
    return path

def get_args():
    parser = argparse.ArgumentParser(prog="decon",
                            description="WSI color deconvolution tool")
    parser.add_argument(
        "-i", "--input", 
        dest="input",
        help="Path to the input CZI file",
        metavar="PATH",
        type=is_valid_file_or_directory,
        required=True
        )
    parser.add_argument(
        "-o", "--output", 
        dest="output",
        help="Path to the output directory",
        metavar="DIR",
        required=True
        )

    return parser.parse_args()

def iterate_bboxes(image_width, image_height, tile_size, overlap):
    for y in range(0, image_height, int(tile_size*(1-overlap))):
        for x in range(0, image_width, int(tile_size*(1-overlap))):
            # Define bounding box coordinates
            bbox = (x, y, min(x + tile_size, image_width), min(y + tile_size, image_height))
            yield bbox

def main(args):
    SCENE_IDX=1
    CH_IDX=1
    TAIL=1
    SKIP_SEG=True
    BACKGROUND = 1200
    PARALLEL=True
    tile_size = 6144
    overlap=0.0

    # img = AICSImage(args.input)
    # img.set_scene(SCENE_IDX)
    # img_dask = img.get_image_dask_data("CYX")
    # img_dask.persist()
    img_np = io.czi_read(args.input,scene=0,skip=1)
    pps = io.get_czi_physical_pixel_size(args.input)

    if not SKIP_SEG:
        # img_dask_rescaled = img_dask[CH_IDX,::SUBSAMPLE,::SUBSAMPLE]

        # # get modal value for background
        # bg = stats.mode(img_dask_rescaled, axis=None, keepdims=False)[0]
        # print("Detected modal intensity for background: {}".format(bg))

        # quick masking, only use for lower and upper percentile calculation
        img_dask_masked = img_np[CH_IDX,:,:]
        pl, pu = np.percentile(np.ravel(img_dask_masked[img_dask_masked > BACKGROUND]), (TAIL, 100-TAIL))
        
        if not isinstance(pl, (int,float)):
            pl = pl.compute()
        if not isinstance(pu, (int,float)):
            pu = pu.compute()

        print("Percentiles: ({:.2f},{:.2f})".format(pl,pu))

        # segmentation
        print("Rescaling image intensity...")
        frame_rescaled = exposure.rescale_intensity(img_np[CH_IDX,:,:], in_range=(pl, pu),out_range=(0,1))

        pred = unet_predictions(frame_rescaled[np.newaxis,:,:],"lightsheet_2D_unet_root_ds1x",patch=[1,2048,2048])

        # apply tissue mask for edge data
        pred_masked = pred[0,:,:]
        pred_masked[img_dask_masked < BACKGROUND] = 0

        # save report
        print("Saving segmentation labels...")
        os.makedirs(args.output,exist_ok=True)
        frame_rescaled_ = frame_rescaled.T[:,:,np.newaxis]*(2**16-1)
        io.pyramidal_ome_tiff_write(frame_rescaled_.astype(np.uint16), os.path.join(args.output,"img.ome.tif"), resX=pps.X, resY=pps.Y)
        io.pyramidal_ome_tiff_write(pred_masked.T[:,:,np.newaxis].astype(np.float32), os.path.join(args.output,"pred.ome.tif"), resX=pps.X, resY=pps.Y)
    
    if SKIP_SEG:
        print("Skipping segmentation, load label directly")
        pred = tifffile.imread(os.path.join(args.output,"pred.ome.tif")).T[np.newaxis,:,:]
        frame_rescaled_ = tifffile.imread(os.path.join(args.output,"img.ome.tif")).T

    param_grid = {
        "beta": [ round(x,1) for x in np.arange(0.9,1.01,0.05)],
        "post_minsize": [ round(x,1) for x in np.arange(90,91,10)],
    }

    params = list(ParameterGrid(param_grid))

    image_width, image_height = img_np.shape[1], img_np.shape[2]
    
    bboxes = iterate_bboxes(image_width, image_height, tile_size, overlap)
    bboxes = [bbox for bbox in bboxes][:]

    for param in tqdm(params, desc="Post processing"):
        beta = param["beta"]
        post_minsize = param["post_minsize"]
        tqdm.write("Beta: {}, Post MinSize: {}".format(beta,post_minsize))

        # limited ram
        if PARALLEL:
            mask = np.zeros_like(pred[0,:,:],dtype=np.uint16)
            
            pool = multiprocessing.Pool(processes=multiprocessing.cpu_count())

            results = []
            
            data_to_process = []

            with tqdm(total=len(bboxes),desc="Preparing watershed tiles") as pbar1:
                for bbox in bboxes:
                    x0, y0, x1, y1 = bbox
                    pred_ = np.copy(pred[0,x0:x1,y0:y1])
                    if np.sum(pred_) < 0.001:
                        pbar1.update(1)
                        continue
                    data_to_process.append({"bbox":bbox,"pred":pred_})
                    pbar1.update(1)

            with tqdm(total=len(bboxes), desc="Processing tiled watershed") as pbar:
                def progress_update(res):
                    pbar.update(1)
                
                def error_callback(err):
                    print(err)

                for data in data_to_process:
                    pred_ = data["pred"]
                    res = pool.apply_async(mutex_ws, (pred_,), {"superpixels": None, "beta": beta, "post_minsize": post_minsize, "n_threads": 6},callback=progress_update,error_callback=error_callback)
                    results.append({"res":res, "bbox": data["bbox"]})

                for res in results:
                    x0, y0, x1, y1 = res["bbox"]
                    mask[x0:x1,y0:y1] = res["res"].get().astype(np.uint16)

                pool.close()
                pool.join()
        else:
            mask = mutex_ws(pred,superpixels=None,beta=beta,post_minsize=post_minsize,n_threads=multiprocessing.cpu_count())

        # mask_relab, fw, inv = relabel_sequential(mask)
        # outlines = utils.masks_to_outlines(mask_relab)
        outlines = utils.masks_to_outlines(mask)

        outX, outY = np.nonzero(outlines)
        img0 = image_to_rgb(frame_rescaled_, channels=[0,0])
        imgout= img0.copy()
        imgout[outX, outY] = np.array([255,0,0]) # pure red

        # save watershed results
        out_dir_ = os.path.join(args.output,"beta-{}_pms-{}".format(beta, post_minsize))
        os.makedirs(out_dir_,exist_ok=True)
        # OmeTiffWriter.save(mask_relab,os.path.join(out_dir_,"mask.ome.tif"),dim_order="YX")
        # OmeTiffWriter.save(imgout,os.path.join(out_dir_,"overlay.ome.tif"),dim_order="YXS")

        io.pyramidal_ome_tiff_write(mask.astype(np.uint16).T[:,:,np.newaxis], os.path.join(out_dir_,"mask_relab.ome.tif"), resX=pps.X, resY=pps.Y)
        io.pyramidal_ome_tiff_write(np.transpose(imgout,(1,0,2)).astype(np.uint8), os.path.join(out_dir_,"overlay.ome.tif"), resX=pps.X, resY=pps.Y)

if __name__ == "__main__":
    args = get_args()
    main(args)