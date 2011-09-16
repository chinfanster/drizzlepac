import os
import numpy as np
import pywcs
import stwcs
from stwcs import wcsutil
import pyfits
import stsci.imagestats as imagestats

import sextractor
from sextractor import is_installed

#import idlphot
import tweakutils,util

try:
    from matplotlib import pyplot as pl
except:
    pl = None

COLNAME_PARS = ['xcol','ycol','fluxcol']
CATALOG_ARGS = ['sharpcol','roundcol','hmin','fwhm','fluxmax','fluxmin','fluxunits','nbright']+COLNAME_PARS

REFCOL_PARS = ['refxcol','refycol','rfluxcol']
REFCAT_ARGS = ['rfluxmax','rfluxmin','rfluxunits','refnbright']+REFCOL_PARS

def generateCatalog(wcs,mode='automatic',catalog=None,**kwargs):
    """ Function which determines what type of catalog object needs to be
        instantiated based on what type of source selection algorithm the user
        specified.

        Parameters
        ----------
        wcs : obj
            WCS object generated by STWCS or PyWCS
        catalog : str or ndarray
            Filename of existing catalog or ndarray of image for generation of source catalog
        kwargs : dict
            Parameters needed to interpret source catalog from input catalog
            with `findmode` being required.

        Returns
        -------
        catalog : obj
            A Catalog-based class instance for keeping track of WCS and
            associated source catalog
    """
    if not isinstance(catalog,Catalog):
        if mode == 'automatic': # if an array is provided as the source
            # Create a new catalog directly from the image
            if kwargs['findmode'] == 'sextractor' and is_installed():
                catalog = ExtractorCatalog(wcs,catalog,**kwargs)
                #catalog = ImageCatalog(wcs,catalog,**kwargs)
            else:
                catalog = ImageCatalog(wcs,catalog,**kwargs)
        else: # a catalog file was provided as the catalog source
            catalog = UserCatalog(wcs,catalog,**kwargs)
    return catalog

class Catalog(object):
    """ Base class for keeping track of a source catalog for an input WCS

        .. warning:: This class should never be instantiated by itself,
                     as necessary methods are not defined yet.
    """
    def __init__(self,wcs,catalog_source,**kwargs):
        """
        This class requires the input of a WCS and a source for the catalog,
        along with any arguments necessary for interpreting the catalog.


        Parameters
        ----------
        wcs : obj
            Input WCS object generated using STWCS or HSTWCS
        catalog_source : str or ndarray
            Catalog generated from this image(ndarray) or read from this file(str)
        kwargs : dict
            Parameters for interpreting the catalog file or for performing the source
            extraction from the image. These will be set differently depending on
            the type of catalog being instantiated.
        """
        self.wcs = wcs # could be None in case of user-supplied catalog
        self.xypos = None
        self.in_units = 'pixels'
        self.sharp = None
        self.round = None
        self.numcols = None
        self.origin = 1 # X,Y coords will ALWAYS be FITS 1-based, not numpy 0-based
        self.pars = kwargs

        self.start_id = 0
        if self.pars.has_key('start_id'): 
            self.start_id = self.pars['start_id']

        self.fname = catalog_source
        self.source = catalog_source
        self.catname = None

        self.num_objects = None
        
        self.radec = None # catalog of sky positions for all sources on this chip/image
        self.set_colnames()

    def generateXY(self):
        """ Method to generate source catalog in XY positions
            Implemented by each subclass
        """
        pass

    def set_colnames(self):
        """ Method to define how to interpret a catalog file
            Only needed when provided a source catalog as input
        """
        pass

    def _readCatalog(self):
        pass

    def generateRaDec(self):
        """ Convert XY positions into sky coordinates using STWCS methods
        """
        if not isinstance(self.wcs,pywcs.WCS):
            print 'WCS not a valid PyWCS object. Conversion of RA/Dec not possible...'
            raise ValueError
        if len(self.xypos[0]) == 0:
            self.xypos = None
        if self.xypos is None:
            print 'No objects found for this image from catalog: ',self.source
            return

        if self.radec is None or force:
            if self.wcs is not None:
                print 'Number of objects in catalog: ',len(self.xypos[0])
                self.radec = self.wcs.all_pix2sky(self.xypos[0],self.xypos[1],self.origin)
            else:
                # If we have no WCS, simply pass along the XY input positions
                # under the assumption they were already sky positions.
                self.radec = self.xypos

    def buildCatalogs(self):
        """ Primary interface to build catalogs based on user inputs.
        """
        self.generateXY()
        self.generateRaDec()

    def plotXYCatalog(self,**kwargs):
        """
        Method which displays the original image and overlays the positions
        of the detected sources from this image's catalog.

        Plotting `kwargs` that can be provided are::

            vmin, vmax, cmap, marker

        Default colormap is `summer`.

        """
        if pl is not None: # If the pyplot package could be loaded...
            pl.clf()
            pars = kwargs.copy()

            if not pars.has_key('marker'):
                pars['marker'] = 'b+'

            if pars.has_key('cmap'):
                pl_cmap = pars['cmap']
                del pars['cmap']
            else:
                pl_cmap = 'summer'
            pl_vmin = None
            pl_vmax = None
            if pars.has_key('vmin'):
                pl_vmin = pars['vmin']
                del pars['vmin']
            if pars.has_key('vmax'):
                pl_vmax = pars['vmax']
                del pars['vmax']

            pl.imshow(self.source,cmap=pl_cmap,vmin=pl_vmin,vmax=pl_vmax)
            pl.plot(self.xypos[0]-1,self.xypos[1]-1,pars['marker'])

    def writeXYCatalog(self,filename):
        """ Write out the X,Y catalog to a file
        """
        if self.xypos is None:
            print 'No X,Y source catalog to write to file. '
            return

        f = open(filename,'w')
        f.write("# Source catalog derived for %s\n"%self.wcs.filename)
        f.write("# Columns: \n")
        f.write('#    X      Y         Flux       ID\n')
        f.write('#   (%s)   (%s)\n'%(self.in_units,self.in_units))

        for row in range(len(self.xypos[0])):
            for i in range(len(self.xypos)):
                f.write("%g  "%(self.xypos[i][row]))
            f.write("\n")

        f.close()


class ExtractorCatalog(Catalog):
    """ Class which generates a source catalog from an image using
        Sextractor as installed by the user on their system.

    """
    SEXTRACTOR_OUTPUT = ["X_IMAGE", "Y_IMAGE", "FLUX_BEST", "FLUXERR_BEST","FLAGS", "FWHM_IMAGE","NUMBER"]
    SEXTRACTOR_BOOL = {True:'Y', False:'N'}

    def _convert_MEF2SF(self):
        indx = self.source.find('[')
        if indx > 0:
            rootname = self.source[:indx]
            lindx = self.source.find(']')
            extn = int(self.source[indx+1:lindx])
        else:
            rootname = self.source
            extn = 0
        fimg = pyfits.open(rootname)
        extver = fimg[extn].header['extver']
        new_fname = rootname[:rootname.rfind('.fits')]+'_extract_sci'+str(extver)+'.fits'
        if os.path.exists(new_fname): os.remove(new_fname)
        phdu = pyfits.PrimaryHDU(data=fimg['sci',extver].data,header=fimg['sci',extver].header)
        phdu.writeto(new_fname)
        del phdu
        return new_fname

    def generateXY(self,gain=1.0):

        seobjects = []

        imgname = self._convert_MEF2SF()

        catname = imgname[:imgname.rfind('.fits')]+'_sextractor.cat'
        self.catname = catname
        sextractor_pars = self.pars['USER SUPPLIED PARAMETERS']
        fluxmin = sextractor_pars['fluxmin']
        fluxmax = sextractor_pars['fluxmax']
        execname = self.pars['execname']
        if util.is_blank(execname): execname = None

        # Create a SExtractor instance
        s = sextractor.SExtractor()

        # Modify the SExtractor configuration
        s.config['GAIN'] = gain
        s.config['PIXEL_SCALE'] = self.wcs.pscale
        s.config['CATALOG_NAME'] = catname
        # Add a parameter to the parameter list
        s.config['PARAMETERS_LIST'] = self.SEXTRACTOR_OUTPUT
        
        # Now, build user-specified filter_mask (if requested)
        if sextractor_pars['filter'] and sextractor_pars['filter_type'] != 'point':
            # apply user-specified values
            new_kernel = tweakutils.gauss_array(sextractor_pars['gauss_nx'],
                        fwhm=sextractor_pars['gauss_fwhm'])
            s.config['FILTER_MASK'] = new_kernel.tolist()
            s.config['FILTER_MASK_DESCR'] = "convolution mask of a gaussian PSF with FWHM = %0.1f pixels.\n"%sextractor_pars['gauss_fwhm']

        if util.is_blank(sextractor_pars['config_file']):
            # Update in-memory parameters as set by user
            for key in sextractor_pars:
                # convert booleans returned by configObj/Python in Y/N values
                # used by SExtractor
                if isinstance(sextractor_pars[key], bool):
                    sextractor_pars[key] = self.SEXTRACTOR_BOOL[sextractor_pars[key]]
                # Populate the SExtractor object with values provided by user
                if sextractor_pars[key] != "" and key not in ['$nargs',
                        'mode','config_file','fluxmin','fluxmax',
                        'gauss_nx','gauss_fwhm','filter_type']:
                    if key != 'phot_autoparams':
                        s.config[key.upper()] = sextractor_pars[key]
                    else:
                        vals = []
                        for v in sextractor_pars[key].split(','):
                            vals.append(float(v))
                        s.config[key.upper()] = vals
            s.update_config()
        else:
            s.update_config()
            s.config['CONFIG_FILE'] = sextractor_pars['config_file']

        try:
            print 'Running SExtractor on ',self.source
            # Lauch SExtractor on a FITS file
            s.run(imgname,path=execname,verbose=True)
            seobjects.append(s)

            # Removing the configuration files, the catalog and
            # the check image
            s.clean(config=False, catalog=False, check=True)
            # also clean up the intermediate file that was generated
            os.remove(imgname)

        except sextractor.SExtractorException:
            print 'WARNING: Problem running the SExtractor executable.'
            if os.path.exists(catname):
                os.remove(catname)
            del s
            return

        """ Read object catalog produced by SExtractor and
            return positions for specific chip extension.

        """
        secatalog = seobjects[0].catalog()
        x = []
        y = []
        fluxes = []
        for obj in secatalog:
            x.append(obj['X_IMAGE'])
            y.append(obj['Y_IMAGE'])
            fluxes.append(obj['FLUX_BEST'])
        self.xypos = [np.array(x),np.array(y),np.array(fluxes),np.arange(len(x))+self.start_id] 
        self.in_units = 'pixels' # Not strictly necessary, but documents units when determined
        self.sharp = None # sharp
        self.round = None # round
        self.numcols = 3  # 5
        self.num_objects = len(x)
        
class ImageCatalog(Catalog):
    """ Class which generates a source catalog from an image using
        Python-based, daofind-like algorithms

        Required input `kwargs` parameters::

            computesig, sigma, threshold, datamin, datamax,
            hmin, fwhmpsf, [roundlim, sharplim]

    """
    def __init__(self,wcs,catalog_source,**kwargs):
        Catalog.__init__(self,wcs,catalog_source,**kwargs)
        if self.wcs.extname == ('',None): self.wcs.extname = (0)
        self.source = pyfits.getdata(self.wcs.filename,ext=self.wcs.extname)

    def generateXY(self):
        """ Generate source catalog from input image using DAOFIND-style algorithm
        """
        #x,y,flux,sharp,round = idlphot.find(array,self.pars['hmin'],self.pars['fwhm'],
        #                    roundlim=self.pars['roundlim'], sharplim=self.pars['sharplim'])
        if self.pars['computesig']:
            # compute sigma for this image
            sigma = self._compute_sigma()
        else:
            sigma = self.pars['sigma']
        hmin = sigma * self.pars['threshold']
        print 'hmin=',hmin,' based on sigma=',sigma,' and threshold = ',self.pars['threshold']
        if self.pars.has_key('datamin') and self.pars['datamin'] is not None:
            source = np.where(self.source <= self.pars['datamin'], 0.,self.source)
        else:
            source = self.source
        
        x,y,flux,id = tweakutils.ndfind(source,hmin,self.pars['fwhmpsf'],datamax=self.pars['datamax'])
        if len(x) == 0:
            sigma = self._compute_sigma()
            hmin = sigma * self.pars['threshold']
            print 'No sources found with original thresholds. Trying automatic settings.'
            x,y,flux,id = tweakutils.ndfind(source,hmin,self.pars['fwhmpsf'],datamax=self.pars['datamax'])
        
        if self.pars.has_key('fluxmin') and self.pars['fluxmin'] is not None:
            fminindx = flux >= self.pars['fluxmin']
        else:
            fminindx = flux == flux
        if self.pars.has_key('fluxmax') and self.pars['fluxmax'] is not None:
            fmaxindx = flux <= self.pars['fluxmax']
        else:
            fmaxindx = flux == flux
        findx = np.bitwise_and(fminindx,fmaxindx)
                
        self.xypos = [x[findx]+1,y[findx]+1,flux[findx],id[findx]+self.start_id] # convert the positions from numpy 0-based to FITS 1-based
        self.in_units = 'pixels' # Not strictly necessary, but documents units when determined
        self.sharp = None # sharp
        self.round = None # round
        self.numcols = 3  # 5
        self.num_objects = len(x)

    def _compute_sigma(self):
        istats = imagestats.ImageStats(self.source,nclip=3,fields='mode,stddev')
        sigma = 1.5 * istats.stddev
        return sigma
        
class UserCatalog(Catalog):
    """ Class to manage user-supplied catalogs as inputs.

        Required input `kwargs` parameters::

            xyunits, xcol, ycol[, fluxcol, [idcol]]

    """
    COLNAMES = COLNAME_PARS
    IN_UNITS = None

    def set_colnames(self):
        self.colnames = []

        cnum = 1
        for cname in self.COLNAMES:
            if cname in self.pars and not util.is_blank(self.pars[cname]):
                self.colnames.append(self.pars[cname])
            else:
                # Insure that at least x and y columns had default values
                if 'fluxcol' not in cname:
                    self.colnames.append(str(cnum))
                cnum += 1

        # count the number of columns
        self.numcols = len(self.colnames)

        if self.IN_UNITS is not None:
            self.in_units = self.IN_UNITS
        else:
            self.in_units = self.pars['xyunits']

    def _readCatalog(self):
        # define what columns will be read
        # The following loops
        #colnums = [self.pars['xcol']-1,self.pars['ycol']-1,self.pars['fluxcol']-1]

        # read the catalog now, one for each chip/mosaic
        # Currently, this only supports ASCII catalog files
        # Support for FITS tables needs to be added        
        catcols = tweakutils.readcols(self.source, cols=self.colnames)
        if len(catcols[0]) == 0:
            catcols = None
        return catcols

    def generateXY(self):
        """
        Method to interpret input catalog file as columns of positions and fluxes.
        """

        xycols = self._readCatalog()
        if xycols is not None:
            # convert the catalog into attribute
            self.xypos = xycols[:3]
            # convert optional columns if they are present
            if self.numcols > 3:
                self.sharp = xycols[3]
            if self.numcols > 4:
                self.round = xycols[4]

        self.num_objects = 0
        if xycols is not None:
            self.num_objects = len(xycols[0])

    def plotXYCatalog(self,**kwargs):
        """
        Plots the source catalog positions using matplotlib's `pyplot.plot()`

        Plotting `kwargs` that can also be passed include any keywords understood
        by matplotlib's `pyplot.plot()` function such as::

            vmin, vmax, cmap, marker


        """
        if pl is not None:
            pl.clf()
            pl.plot(self.xypos[0],self.xypos[1],**kwargs)


class RefCatalog(UserCatalog):
    """ Class which manages a reference catalog.

    Notes
    -----
    A *reference catalog* is defined as a catalog of undistorted source positions
    given in RA/Dec which would be used as the master list for subsequent
    matching and fitting.

    """
    COLNAMES = REFCOL_PARS
    IN_UNITS = 'degrees'

    def generateXY(self):
        pass
    def generateRaDec(self):
        if isinstance(self.source,list):
            self.radec = self.source
        else:
            self.radec = self._readCatalog()
