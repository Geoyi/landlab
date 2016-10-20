from __future__ import print_function

import warnings

from six.moves import range

import numpy as np
from time import sleep
from landlab import ModelParameterDictionary, CLOSED_BOUNDARY, Component

from landlab.core.model_parameter_dictionary import MissingKeyError
from landlab.field.scalar_data_fields import FieldError
from landlab.grid.base import BAD_INDEX_VALUE
from landlab.utils.decorators import make_return_array_immutable


class SedDepEroder(Component):
    """
    This module implements sediment flux dependent channel incision
    following::

        E = f(Qs, Qc) * ([a stream power-like term] - [an optional threshold]),

    where E is the bed erosion rate, Qs is the volumetric sediment flux
    into a node, and Qc is the volumetric sediment transport capacity at
    that node.

    This component is under active research and development; proceed with its
    use at your own risk.

    The details of the implementation are a function of the two key
    arguments, *sed_dependency_type* and *Qc*. The former controls the
    shape of the sediment dependent response function f(Qs, Qc), the
    latter controls the way in which sediment transport capacities are
    calculated.
    ***Note that this new implementation only permits a power_law formulation
    for Qc***
    For Qc, 'power_law' broadly follows the assumptions in Gasparini et
    al. 2006, 2007.

    If ``Qc == 'power_law'``::

        E  = K_sp * f(Qs, Qc) * A ** m_sp * S ** n_sp;
        Qc = K_t * A ** m_t * S ** n_t

    The component is able to handle flooded nodes, if created by a lake
    filler. It assumes the flow paths found in the fields already reflect
    any lake routing operations, and then requires the optional argument
    *flooded_depths* be passed to the run method. A flooded depression
    acts as a perfect sediment trap, and will be filled sequentially
    from the inflow points towards the outflow points.

    Construction::

        SedDepEroder(grid, K_sp=1.e-6, g=9.81, rock_density=2700,
                     sediment_density=2700, fluid_density=1000,
                     runoff_rate=1.,
                     sed_dependency_type='generalized_humped',
                     kappa_hump=13.683, nu_hump=1.13, phi_hump=4.24,
                     c_hump=0.00181, Qc='power_law', m_sp=0.5, n_sp=1.,
                     K_t=1.e-4, m_t=1.5, n_t=1.,
                     pseudoimplicit_repeats=5,
                     return_stream_properties=False)

    Parameters
    ----------
    grid : a ModelGrid
        A grid.
    K_sp : float (time unit must be *years*)
        K in the stream power equation; the prefactor on the erosion
        equation (units vary with other parameters).
    g : float (m/s**2)
        Acceleration due to gravity.
    rock_density : float (Kg m**-3)
        Bulk intact rock density.
    sediment_density : float (Kg m**-3)
        Typical density of loose sediment on the bed.
    fluid_density : float (Kg m**-3)
        Density of the fluid.
    runoff_rate : float, array or field name (m/s)
        The rate of excess overland flow production at each node (i.e.,
        rainfall rate less infiltration).
    pseudoimplicit_repeats : int
        Number of loops to perform with the pseudoimplicit iterator,
        seeking a stable solution. Convergence is typically rapid.
    return_stream_properties : bool
        Whether to perform a few additional calculations in order to set
        the additional optional output fields, 'channel__width',
        'channel__depth', and 'channel__discharge' (default False).
    sed_dependency_type : {'generalized_humped', 'None', 'linear_decline',
                           'almost_parabolic'}
        The shape of the sediment flux function. For definitions, see
        Hobley et al., 2011. 'None' gives a constant value of 1.
        NB: 'parabolic' is currently not supported, due to numerical
        stability issues at channel heads.
    Qc : {'power_law', 'MPM'}
        Whether to use simple stream-power-like equations for both
        sediment transport capacity and erosion rate, or more complex
        forms based directly on the Meyer-Peter Muller equation and a
        shear stress based erosion model consistent with MPM (per
        Hobley et al., 2011).

    If ``sed_dependency_type == 'generalized_humped'``...

    kappa_hump : float
        Shape parameter for sediment flux function. Primarily controls
        function amplitude (i.e., scales the function to a maximum of 1).
        Default follows Leh valley values from Hobley et al., 2011.
    nu_hump : float
        Shape parameter for sediment flux function. Primarily controls
        rate of rise of the "tools" limb. Default follows Leh valley
        values from Hobley et al., 2011.
    phi_hump : float
        Shape parameter for sediment flux function. Primarily controls
        rate of fall of the "cover" limb. Default follows Leh valley
        values from Hobley et al., 2011.
    c_hump : float
        Shape parameter for sediment flux function. Primarily controls
        degree of function asymmetry. Default follows Leh valley values
        from Hobley et al., 2011.

    If ``Qc == 'power_law'``...

    m_sp : float
        Power on drainage area in the erosion equation.
    n_sp : float
        Power on slope in the erosion equation.
    K_t : float (time unit must be in *years*)
        Prefactor in the transport capacity equation.
    m_t : float
        Power on drainage area in the transport capacity equation.
    n_t : float
        Power on slope in the transport capacity equation.

    """

    _name = 'SedDepEroder'

    _input_var_names = (
        'topographic__elevation',
        'drainage_area',
        'flow__receiver_node',
        'flow__upstream_node_order',
        'topographic__steepest_slope',
        'flow__link_to_receiver_node'
    )

    _output_var_names = (
        'topographic__elevation',
        'channel__bed_shear_stress',
        'channel_sediment__volumetric_transport_capacity',
        'channel_sediment__volumetric_flux',
        'channel_sediment__relative_flux',
        'channel__discharge',
        'channel__width',  # optional
        'channel__depth',  # optional
    )

    _optional_var_names = (
        'channel__width',
        'channel__depth'
    )

    _var_units = {'topographic__elevation': 'm',
                  'drainage_area': 'm**2',
                  'flow__receiver_node': '-',
                  'topographic__steepest_slope': '-',
                  'flow__upstream_node_order': '-',
                  'flow__link_to_receiver_node': '-',
                  'channel__bed_shear_stress': 'Pa',
                  'channel_sediment__volumetric_transport_capacity': 'm**3/s',
                  'channel_sediment__volumetric_flux': 'm**3/s',
                  'channel_sediment__relative_flux': '-',
                  'channel__discharge': 'm**3/s',
                  'channel__width': 'm',
                  'channel__depth': 'm'
                  }

    _var_mapping = {'topographic__elevation': 'node',
                    'drainage_area': 'node',
                    'flow__receiver_node': 'node',
                    'topographic__steepest_slope': 'node',
                    'flow__upstream_node_order': 'node',
                    'flow__link_to_receiver_node': 'node',
                    'channel__bed_shear_stress': 'node',
                    'channel_sediment__volumetric_transport_capacity': 'node',
                    'channel_sediment__volumetric_flux': 'node',
                    'channel_sediment__relative_flux': 'node',
                    'channel__discharge': 'node',
                    'channel__width': 'node',
                    'channel__depth': 'node'
                    }

    _var_type = {'topographic__elevation': float,
                 'drainage_area': float,
                 'flow__receiver_node': int,
                 'topographic__steepest_slope': float,
                 'flow__upstream_node_order': int,
                 'flow__link_to_receiver_node': int,
                 'channel__bed_shear_stress': float,
                 'channel_sediment__volumetric_transport_capacity': float,
                 'channel_sediment__volumetric_flux': float,
                 'channel_sediment__relative_flux': float,
                 'channel__discharge': float,
                 'channel__width': float,
                 'channel__depth': float
                 }

    _var_doc = {
        'topographic__elevation': 'Land surface topographic elevation',
        'drainage_area':
            ("Upstream accumulated surface area contributing to the node's " +
             "discharge"),
        'flow__receiver_node':
            ('Node array of receivers (node that receives flow from current ' +
             'node)'),
        'topographic__steepest_slope':
            'Node array of steepest *downhill* slopes',
        'flow__upstream_node_order':
            ('Node array containing downstream-to-upstream ordered list of ' +
             'node IDs'),
        'flow__link_to_receiver_node':
            'ID of link downstream of each node, which carries the discharge',
        'channel__bed_shear_stress':
            ('Shear exerted on the bed of the channel, assuming all ' +
             'discharge travels along a single, self-formed channel'),
        'channel_sediment__volumetric_transport_capacity':
            ('Volumetric transport capacity of a channel carrying all runoff' +
             ' through the node, assuming the Meyer-Peter Muller transport ' +
             'equation'),
        'channel_sediment__volumetric_flux':
            ('Total volumetric fluvial sediment flux brought into the node ' +
             'from upstream'),
        'channel_sediment__relative_flux':
            ('The fluvial_sediment_flux_into_node divided by the fluvial_' +
             'sediment_transport_capacity'),
        'channel__discharge':
            ('Volumetric water flux of the a single channel carrying all ' +
             'runoff through the node'),
        'channel__width':
            ('Width of the a single channel carrying all runoff through the ' +
             'node'),
        'channel__depth':
            ('Depth of the a single channel carrying all runoff through the ' +
             'node')
    }

    def __init__(self, grid, K_sp=1.e-6, g=9.81,
                 rock_density=2700, sediment_density=2700, fluid_density=1000,
                 runoff_rate=1.,
                 sed_dependency_type='generalized_humped', kappa_hump=13.683,
                 nu_hump=1.13, phi_hump=4.24, c_hump=0.00181,
                 Qc='power_law', m_sp=0.5, n_sp=1., K_t=1.e-4, m_t=1.5, n_t=1.,
                 # params for model numeric behavior:
                 pseudoimplicit_repeats=5, return_stream_properties=False,
                 **kwds):
        """Constructor for the class."""
        self._grid = grid
        self.pseudoimplicit_repeats = pseudoimplicit_repeats

        self.link_S_with_trailing_blank = np.zeros(grid.number_of_links+1)
        # ^needs to be filled with values in execution
        self.count_active_links = np.zeros_like(
            self.link_S_with_trailing_blank, dtype=int)
        self.count_active_links[:-1] = 1

        self._K_unit_time = K_sp/31557600.
        # ^...because we work with dt in seconds
        # set gravity
        self.g = g
        self.rock_density = rock_density
        self.sed_density = sediment_density
        self.fluid_density = fluid_density
        self.relative_weight = (
            (self.sed_density-self.fluid_density)/self.fluid_density*self.g)
        # ^to accelerate MPM calcs
        self.rho_g = self.fluid_density*self.g
        self.type = sed_dependency_type
        assert self.type in ('generalized_humped', 'None', 'linear_decline',
                             'almost_parabolic')
        self.Qc = Qc
        assert self.Qc in ('MPM', 'power_law')
        self.return_ch_props = return_stream_properties
        if return_stream_properties:
            assert(self.Qc == 'MPM', "Qc must be 'MPM' to return stream " +
                   "properties")
        if type(runoff_rate) in (float, int):
            self.runoff_rate = float(runoff_rate)
        elif type(runoff_rate) is str:
            self.runoff_rate = self.grid.at_node[runoff_rate]
        else:
            self.runoff_rate = np.array(runoff_rate)
            assert runoff_rate.size == self.grid.number_of_nodes

        if self.Qc == 'MPM':
            raise TypeError('MPM is no longer a permitted value for Qc!')
        elif self.Qc == 'power_law':
            self._m = m_sp
            self._n = n_sp
            self._Kt = K_t/31557600.  # in sec
            self._mt = m_t
            self._nt = n_t

        # now conditional inputs
        if self.type == 'generalized_humped':
            self.kappa = kappa_hump
            self.nu = nu_hump
            self.phi = phi_hump
            self.c = c_hump

        self.cell_areas = np.empty(grid.number_of_nodes)
        self.cell_areas.fill(np.mean(grid.area_of_cell))
        self.cell_areas[grid.node_at_cell] = grid.area_of_cell

        # set up the necessary fields:
        self.initialize_output_fields()
        if self.return_ch_props:
            self.initialize_optional_output_fields()

    def get_sed_flux_function(self, rel_sed_flux):
        if self.type == 'generalized_humped':
            "Returns K*f(qs,qc)"
            sed_flux_fn = self.kappa*(rel_sed_flux**self.nu + self.c)*np.exp(
                -self.phi*rel_sed_flux)
        elif self.type == 'linear_decline':
            sed_flux_fn = (1.-rel_sed_flux)
        elif self.type == 'parabolic':
            raise MissingKeyError(
                'Pure parabolic (where intersect at zero flux is exactly ' +
                'zero) is currently not supported, sorry. Try ' +
                'almost_parabolic instead?')
            sed_flux_fn = 1. - 4.*(rel_sed_flux-0.5)**2.
        elif self.type == 'almost_parabolic':
            sed_flux_fn = np.where(rel_sed_flux > 0.1,
                                   1. - 4.*(rel_sed_flux-0.5)**2.,
                                   2.6*rel_sed_flux+0.1)
        elif self.type == 'None':
            sed_flux_fn = 1.
        else:
            raise MissingKeyError(
                'Provided sed flux sensitivity type in input file was not ' +
                'recognised!')
        return sed_flux_fn

    def get_sed_flux_function_pseudoimplicit_old(self, sed_in, trans_cap_vol_out,
                                             prefactor_for_volume,
                                             prefactor_for_dz):
        rel_sed_flux_in = sed_in/trans_cap_vol_out
        rel_sed_flux = rel_sed_flux_in

        if self.type == 'generalized_humped':
            "Returns K*f(qs,qc)"

            def sed_flux_fn_gen(rel_sed_flux_in):
                return self.kappa*(rel_sed_flux_in**self.nu + self.c)*np.exp(
                    -self.phi*rel_sed_flux_in)

        elif self.type == 'linear_decline':
            def sed_flux_fn_gen(rel_sed_flux_in):
                return 1.-rel_sed_flux_in

        elif self.type == 'parabolic':
            raise MissingKeyError(
                'Pure parabolic (where intersect at zero flux is exactly ' +
                'zero) is currently not supported, sorry. Try ' +
                'almost_parabolic instead?')

            def sed_flux_fn_gen(rel_sed_flux_in):
                return 1. - 4.*(rel_sed_flux_in-0.5)**2.

        elif self.type == 'almost_parabolic':

            def sed_flux_fn_gen(rel_sed_flux_in):
                return np.where(rel_sed_flux_in > 0.1,
                                1. - 4.*(rel_sed_flux_in-0.5)**2.,
                                2.6*rel_sed_flux_in+0.1)

        elif self.type == 'None':

            def sed_flux_fn_gen(rel_sed_flux_in):
                return 1.
        else:
            raise MissingKeyError(
                'Provided sed flux sensitivity type in input file was not ' +
                'recognised!')

        for i in range(self.pseudoimplicit_repeats):
            sed_flux_fn = sed_flux_fn_gen(rel_sed_flux)
            sed_vol_added = prefactor_for_volume*sed_flux_fn
            rel_sed_flux = rel_sed_flux_in + sed_vol_added/trans_cap_vol_out
            # print rel_sed_flux
            if rel_sed_flux >= 1.:
                rel_sed_flux = 1.
                break
            if rel_sed_flux < 0.:
                rel_sed_flux = 0.
                break
        last_sed_flux_fn = sed_flux_fn
        sed_flux_fn = sed_flux_fn_gen(rel_sed_flux)
        # this error could alternatively be used to break the loop
        error_in_sed_flux_fn = sed_flux_fn-last_sed_flux_fn
        dz = prefactor_for_dz*sed_flux_fn
        sed_flux_out = rel_sed_flux*trans_cap_vol_out
        return dz, sed_flux_out, rel_sed_flux, error_in_sed_flux_fn

    def get_sed_flux_function_pseudoimplicit(self, sed_in_bydt,
                                             trans_cap_vol_out_bydt,
                                             prefactor_for_volume_bydt,
                                             prefactor_for_dz_bydt):
        """
        This function uses a pseudoimplicit method to calculate the sediment
        flux function for a node, and also returns dz/dt and the rate of
        sediment output from the node.

        Note that this method now operates in PER TIME units; this was not
        formerly the case.

        Parameters
        ----------
        sed_in_bydt : float
            Total rate of incoming sediment, sum(Q_s_in)/dt
        trans_cap_vol_out_bydt : float
            Volumetric transport capacity as a rate (i.e., m**3/s) on outgoing
            link
        prefactor_for_volume_bydt : float
            Equal to K*A**m*S**n * cell_area
        prefactor_for_dz_bydt : float
            Equal to K*A**m*S**n (both prefactors are passed for computational
            efficiency)

        Returns
        -------
        dzbydt : float
            Rate of change of substrate elevation
        sed_flux_out_bydt : float
            Q_s/dt on the outgoing link
        rel_sed_flux : float
            f(Q_s/Q_c)
        error_in_sed_flux_fn : float
            Measure of how well converged rel_sed_flux is
        """
        rel_sed_flux_in = sed_in_bydt/trans_cap_vol_out_bydt
        rel_sed_flux = rel_sed_flux_in

        if self.type == 'generalized_humped':
            "Returns K*f(qs,qc)"

            def sed_flux_fn_gen(rel_sed_flux_in):
                return self.kappa*(rel_sed_flux_in**self.nu + self.c)*np.exp(
                    -self.phi*rel_sed_flux_in)

        elif self.type == 'linear_decline':
            def sed_flux_fn_gen(rel_sed_flux_in):
                return 1.-rel_sed_flux_in

        elif self.type == 'parabolic':
            raise MissingKeyError(
                'Pure parabolic (where intersect at zero flux is exactly ' +
                'zero) is currently not supported, sorry. Try ' +
                'almost_parabolic instead?')

            def sed_flux_fn_gen(rel_sed_flux_in):
                return 1. - 4.*(rel_sed_flux_in-0.5)**2.

        elif self.type == 'almost_parabolic':

            def sed_flux_fn_gen(rel_sed_flux_in):
                return np.where(rel_sed_flux_in > 0.1,
                                1. - 4.*(rel_sed_flux_in-0.5)**2.,
                                2.6*rel_sed_flux_in+0.1)

        elif self.type == 'None':

            def sed_flux_fn_gen(rel_sed_flux_in):
                return 1.
        else:
            raise MissingKeyError(
                'Provided sed flux sensitivity type in input file was not ' +
                'recognised!')

        for i in range(self.pseudoimplicit_repeats):
            sed_flux_fn = sed_flux_fn_gen(rel_sed_flux)
            sed_vol_added_bydt = prefactor_for_volume_bydt*sed_flux_fn
            rel_sed_flux = (rel_sed_flux_in +
                            sed_vol_added_bydt/trans_cap_vol_out_bydt)
            # print rel_sed_flux
            if rel_sed_flux >= 1.:
                rel_sed_flux = 1.
                break
            if rel_sed_flux < 0.:
                rel_sed_flux = 0.
                break
        last_sed_flux_fn = sed_flux_fn
        sed_flux_fn = sed_flux_fn_gen(rel_sed_flux)
        # this error could alternatively be used to break the loop
        error_in_sed_flux_fn = sed_flux_fn-last_sed_flux_fn
        dzbydt = prefactor_for_dz_bydt*sed_flux_fn
        sed_flux_out_bydt = rel_sed_flux*trans_cap_vol_out_bydt
        return dzbydt, sed_flux_out_bydt, rel_sed_flux, error_in_sed_flux_fn

    def erode(self, dt, flooded_depths=None, **kwds):
        """Erode and deposit on the channel bed for a duration of *dt*.

        Erosion occurs according to the sediment dependent rules specified
        during initialization.

        Parameters
        ----------
        dt : float (years, only!)
            Timestep for which to run the component.
        flooded_depths : array or field name (m)
            Depths of flooding at each node, zero where no lake. Note that the
            component will dynamically update this array as it fills nodes
            with sediment (...but does NOT update any other related lake
            fields).
        """
        grid = self.grid
        node_z = grid.at_node['topographic__elevation']
        node_A = grid.at_node['drainage_area']
        flow_receiver = grid.at_node['flow__receiver_node']
        s_in = grid.at_node['flow__upstream_node_order']
        node_S = grid.at_node['topographic__steepest_slope']

        if type(flooded_depths) is str:
            flooded_depths = mg.at_node[flooded_depths]
            # also need a map of initial flooded conds:
            flooded_nodes = flooded_depths > 0.
        elif type(flooded_depths) is np.ndarray:
            assert flooded_depths.size == self.grid.number_of_nodes
            flooded_nodes = flooded_depths > 0.
            # need an *updateable* record of the pit depths
        else:
            # if None, handle in loop
            flooded_nodes = None
        steepest_link = 'flow__link_to_receiver_node'
        link_length = np.empty(grid.number_of_nodes, dtype=float)
        link_length.fill(np.nan)
        draining_nodes = np.not_equal(grid.at_node[steepest_link],
                                      BAD_INDEX_VALUE)
        core_draining_nodes = np.intersect1d(np.where(draining_nodes)[0],
                                             grid.core_nodes,
                                             assume_unique=True)
        link_length[core_draining_nodes] = grid._length_of_link_with_diagonals[
            grid.at_node[steepest_link][core_draining_nodes]]

        if self.Qc == 'power_law':
            transport_capacity_prefactor_withA = self._Kt * node_A**self._mt
            erosion_prefactor_withA = self._K_unit_time * node_A**self._m
            # ^doesn't include S**n*f(Qc/Qc)
            internal_t = 0.
            break_flag = False
            dt_secs = dt*31557600.
            counter = 0
            rel_sed_flux = np.empty_like(node_A)
            while 1:
                counter += 1
                # print counter
                downward_slopes = node_S.clip(0.)
                # positive_slopes = np.greater(downward_slopes, 0.)
                slopes_tothen = downward_slopes**self._n
                slopes_tothent = downward_slopes**self._nt
                transport_capacities = (transport_capacity_prefactor_withA *
                                        slopes_tothent)
                erosion_prefactor_withS = (
                    erosion_prefactor_withA * slopes_tothen)  # no time, no fqs

                dt_this_step = dt_secs-internal_t
                # ^timestep adjustment is made AFTER the dz calc
#                node_vol_capacities = transport_capacities*dt_this_step

#                sed_into_node = np.zeros(grid.number_of_nodes, dtype=float)
                sed_rate_into_node = np.zeros(grid.number_of_nodes, dtype=float)
                dz = np.zeros(grid.number_of_nodes, dtype=float)
                cell_areas = self.cell_areas
                for i in s_in[::-1]:  # work downstream
                    cell_area = cell_areas[i]
                    if flooded_nodes is not None:
                        flood_depth = flooded_depths[i]
                    else:
                        flood_depth = 0.
                    sed_flux_into_this_node_bydt = sed_rate_into_node[i]
                    node_capacity = transport_capacities[i]
                    # ^we work in volume flux, not volume per se here
#                    node_vol_capacity = node_vol_capacities[i]
                    if flood_depth > 0.:
                        node_capacity = 0.
                    if sed_flux_into_this_node_bydt < node_capacity:
                        # ^note incision is forbidden at capacity
                        dz_prefactor_bydt = erosion_prefactor_withS[i]
                        vol_prefactor_bydt = dz_prefactor_bydt*cell_area
                        (dzbydt_here, sed_flux_out_bydt, rel_sed_flux_here,
                         error_in_sed_flux) = \
                            self.get_sed_flux_function_pseudoimplicit(
                                sed_flux_into_this_node_bydt,
                                node_capacity,
                                vol_prefactor_bydt, dz_prefactor_bydt)
                        # note now dz_here may never create more sed than the
                        # out can transport...
                        assert sed_flux_out_bydt <= node_capacity, (
                            'failed at node '+str(s_in.size-i) +
                            ' with rel sed flux '+str(
                                sed_flux_out_bydt/node_capacity))
                        rel_sed_flux[i] = rel_sed_flux_here
                        vol_pass_rate = sed_flux_out_bydt
                    else:
                        rel_sed_flux[i] = 1.
                        vol_drop_rate = (sed_flux_into_this_node_bydt -
                                       node_capacity)
                        dzbydt_here = -vol_drop_rate/cell_area
                        try:
                            isflooded = flooded_nodes[i]
                        except TypeError:  # was None
                            isflooded = False
                        if flood_depth <= 0. and not isflooded:
                            vol_pass_rate = node_capacity
                            # we want flooded nodes which have already been
                            # filled to enter the else statement
                        else:
########modify ->
                            height_excess = -dz_here - flood_depth
                            # ...above water level
                            if height_excess <= 0.:
                                vol_pass = 0.
                                # dz_here is already correct
                                flooded_depths[i] += dz_here
                            else:
                                dz_here = -flood_depth
                                vol_pass = height_excess * cell_area
                                # ^bit cheeky?
                                flooded_depths[i] = 0.

                    dz[i] -= dzbydt_here * dt_this_step
                    sed_rate_into_node[flow_receiver[i]] += vol_pass_rate
                break_flag = True

                node_z[grid.core_nodes] += dz[grid.core_nodes]

                if break_flag:
                    break
                # do we need to reroute the flow/recalc the slopes here?
                # -> NO, slope is such a minor component of Diff we'll be OK
                # BUT could be important not for the stability, but for the
                # actual calc. So YES.
                node_S = np.zeros_like(node_S)
                # print link_length[core_draining_nodes]
                node_S[core_draining_nodes] = (node_z-node_z[flow_receiver])[
                    core_draining_nodes]/link_length[core_draining_nodes]
                internal_t += dt_this_step  # still in seconds, remember
        else:
            raise TypeError # should never trigger

        active_nodes = grid.core_nodes

        if self.return_ch_props:
            # add the channel property field entries,
            # 'channel__width', 'channel__depth', and 'channel__discharge'
            W = self.k_w*node_Q**self._b
            H = shear_stress/self.rho_g/node_S  # ...sneaky!
            grid.at_node['channel__width'][:] = W
            grid.at_node['channel__depth'][:] = H
            grid.at_node['channel__discharge'][:] = node_Q
            grid.at_node['channel__bed_shear_stress'][:] = shear_stress

        grid.at_node['channel_sediment__volumetric_transport_capacity'][
            :] = transport_capacities
        grid.at_node['channel_sediment__volumetric_flux'][
            :] = sed_rate_into_node
        grid.at_node['channel_sediment__relative_flux'][:] = rel_sed_flux
        # elevs set automatically to the name used in the function call.
        self.iterations_in_dt = counter

        return grid, grid.at_node['topographic__elevation']

    def run_one_step(self, dt, flooded_depths=None, **kwds):
        """Run the component across one timestep increment, dt.

        Erosion occurs according to the sediment dependent rules specified
        during initialization. Method is fully equivalent to the :func:`erode`
        method.

        Parameters
        ----------
        dt : float (years, only!)
            Timestep for which to run the component.
        flooded_depths : array or field name (m)
            Depths of flooding at each node, zero where no lake. Note that the
            component will dynamically update this array as it fills nodes
            with sediment (...but does NOT update any other related lake
            fields).
        """
        self.erode(dt=dt, flooded_depths=flooded_depths, **kwds)
