import math
from datetime import datetime, timedelta
import numpy as np
import sentry_sdk
import logging
import pandas as pd
from scipy import stats as sps
from scipy import signal
from matplotlib import pyplot as plt
import us
import structlog
from pyseir import load_data
from pyseir.utils import AggregationLevel, TimeseriesType
from pyseir.utils import get_run_artifact_path, RunArtifact
from pyseir.parameters.parameter_ensemble_generator import ParameterEnsembleGenerator
import logging
from sklearn import preprocessing


class InferRtConstants:
    RNG_SEED = 42
    LOCAL_LOOKBACK_WINDOW = 14
    Z_THRESHOLD = 10
    MIN_MEAN_TO_CONSIDER = 5


# Small epsilon to prevent divide by 0 errors.
EPSILON = 1e-8


class RtInferenceEngine:
    """
    This class extends the analysis of Bettencourt et al to include mortality
    and hospitalization data in a pseudo-non-parametric inference of R_t.

    Parameters
    ----------
    fips: str
        State or County fips code
    window_size: int
        Size of the sliding Gaussian window to compute. Note that kernel std
        sets the width of the kernel weight.
    kernel_std: int
        Width of the Gaussian kernel.
    r_list: array-like
        Array of R_t to compute posteriors over. Doesn't really need to be
        configured.
    process_sigma: float
        Stdev of the process model. Increasing this allows for larger
        instant deltas in R_t, shrinking it smooths things, but allows for
        less rapid change. Can be interpreted as the std of the allowed
        shift in R_t day-to-day.
    ref_date:
        Reference date to compute from.
    confidence_intervals: list(float)
        Confidence interval to compute. 0.95 would be 90% credible
        intervals from 5% to 95%.
    min_cases: int
        Minimum number of cases required to run case level inference. These are
        very conservatively weak filters, but prevent cases of basically zero
        data from introducing pathological results.
    min_deaths: int
        Minimum number of deaths required to run death level inference.
    include_testing_correction: bool
        If True, include a correction for testing increases and decreases.
    """

    def __init__(
        self,
        fips,
        window_size=14,
        kernel_std=5,
        r_list=np.linspace(0, 10, 501),
        process_sigma=0.05,
        ref_date=datetime(year=2020, month=1, day=1),
        confidence_intervals=(0.68, 0.95),
        min_cases=5,
        min_deaths=5,
        include_testing_correction=True,
    ):
        np.random.seed(InferRtConstants.RNG_SEED)
        # Param Generation used for Xcor in align_time_series, has some stochastic FFT elements.
        self.fips = fips
        self.r_list = r_list
        self.window_size = window_size
        self.kernel_std = kernel_std
        self.process_sigma = process_sigma
        self.ref_date = ref_date
        self.confidence_intervals = confidence_intervals
        self.min_cases = min_cases
        self.min_deaths = min_deaths
        self.include_testing_correction = include_testing_correction

        if len(fips) == 2:  # State FIPS are 2 digits
            self.agg_level = AggregationLevel.STATE
            self.state_obj = us.states.lookup(self.fips)
            self.state = self.state_obj.name

            (
                self.times,
                self.observed_new_cases,
                self.observed_new_deaths,
            ) = load_data.load_new_case_data_by_state(
                self.state,
                self.ref_date,
                include_testing_correction=self.include_testing_correction,
            )
            self.times_raw_new_cases, self.raw_new_cases, _ = load_data.load_new_case_data_by_state(
                self.state, self.ref_date, False
            )
            # logging.info('RAW CASES')
            # logging.info(self.raw_new_cases)

            (
                self.hospital_times,
                self.hospitalizations,
                self.hospitalization_data_type,
            ) = load_data.load_hospitalization_data_by_state(
                state=self.state_obj.abbr, t0=self.ref_date
            )
            self.display_name = self.state
        else:
            self.agg_level = AggregationLevel.COUNTY
            self.geo_metadata = (
                load_data.load_county_metadata().set_index("fips").loc[fips].to_dict()
            )
            self.state = self.geo_metadata["state"]
            self.state_obj = us.states.lookup(self.state)
            self.county = self.geo_metadata["county"]
            if self.county:
                self.display_name = self.county + ", " + self.state
            else:
                self.display_name = self.state

            (
                self.times,
                self.observed_new_cases,
                self.observed_new_deaths,
            ) = load_data.load_new_case_data_by_fips(
                self.fips,
                t0=self.ref_date,
                include_testing_correction=self.include_testing_correction,
            )
            self.times_raw_new_cases, self.raw_new_cases, _ = load_data.load_new_case_data_by_state(
                self.state, self.ref_date, False
            )
            (
                self.hospital_times,
                self.hospitalizations,
                self.hospitalization_data_type,
            ) = load_data.load_hospitalization_data(self.fips, t0=self.ref_date)

        self.case_dates = [ref_date + timedelta(days=int(t)) for t in self.times]
        self.raw_new_case_dates = [
            ref_date + timedelta(days=int(t)) for t in self.times_raw_new_cases
        ]
        if self.hospitalization_data_type:
            self.hospital_dates = [ref_date + timedelta(days=int(t)) for t in self.hospital_times]

        self.default_parameters = ParameterEnsembleGenerator(
            fips=self.fips, N_samples=500, t_list=np.linspace(0, 365, 366)
        ).get_average_seir_parameters()

        # Serial period = Incubation + 0.5 * Infections
        self.serial_period = (
            1 / self.default_parameters["sigma"] + 0.5 * 1 / self.default_parameters["delta"]
        )

        # If we only receive current hospitalizations, we need to account for
        # the outflow to reconstruct new admissions.
        if (
            self.hospitalization_data_type
            is load_data.HospitalizationDataType.CURRENT_HOSPITALIZATIONS
        ):
            los_general = self.default_parameters["hospitalization_length_of_stay_general"]
            los_icu = self.default_parameters["hospitalization_length_of_stay_icu"]
            hosp_rate_general = self.default_parameters["hospitalization_rate_general"]
            hosp_rate_icu = self.default_parameters["hospitalization_rate_icu"]
            icu_rate = hosp_rate_icu / hosp_rate_general
            flow_out_of_hosp = self.hospitalizations[:-1] * (
                (1 - icu_rate) / los_general + icu_rate / los_icu
            )
            # We are attempting to reconstruct the cumulative hospitalizations.
            self.hospitalizations = np.diff(self.hospitalizations) + flow_out_of_hosp
            self.hospital_dates = self.hospital_dates[1:]
            self.hospital_times = self.hospital_times[1:]

        self.log_likelihood = None

        self.log = structlog.getLogger(Rt_Inference_Target=self.display_name)
        self.log.info(event="Running:")

    def get_timeseries(self, timeseries_type):
        """
        Given a timeseries type, return the dates, times, and hospitalizations.

        Parameters
        ----------
        timeseries_type: TimeseriesType
            Which type of time-series to return.

        Returns
        -------
        dates: list(datetime)
            Dates for each observation
        times: list(int)
            Integer days since the reference date.
        timeseries:
            The requested timeseries.
        """
        timeseries_type = TimeseriesType(timeseries_type)

        if timeseries_type is TimeseriesType.NEW_CASES:
            return self.case_dates, self.times, self.observed_new_cases
        elif timeseries_type is TimeseriesType.RAW_NEW_CASES:
            return self.raw_new_case_dates, self.times_raw_new_cases, self.raw_new_cases
        elif timeseries_type is TimeseriesType.NEW_DEATHS or TimeseriesType.RAW_NEW_DEATHS:
            # logging.info('getting deaths')
            # logging.info(self.observed_new_deaths)
            return self.case_dates, self.times, self.observed_new_deaths
        elif timeseries_type in (
            TimeseriesType.NEW_HOSPITALIZATIONS,
            TimeseriesType.CURRENT_HOSPITALIZATIONS,
        ):
            return self.hospital_dates, self.hospital_times, self.hospitalizations

    def apply_gaussian_smoothing(self, timeseries_type, plot=False, smoothed_max_threshold=5):
        """
        Apply a rolling Gaussian window to smooth the data. This signature and
        returns match get_time_series, but will return a subset of the input
        time-series starting at the first non-zero value.

        Parameters
        ----------
        timeseries_type: TimeseriesType
            Which type of time-series to use.
        plot: bool
            If True, plot smoothed and original data.
        smoothed_max_threshold: int
            This parameter allows you to filter out entire series
            (e.g. NEW_DEATHS) when they do not contain high enough
            numeric values. This has been added to account for low-level
            constant smoothed values having a disproportionate effect on
            our final R(t) calculation, when all of their values are below
            this parameter.

        Returns
        -------
        dates: array-like
            Input data over a subset of indices available after windowing.
        times: array-like
            Output integers since the reference date.
        smoothed: array-like
            Gaussian smoothed data.


        """
        timeseries_type = TimeseriesType(timeseries_type)
        dates, times, timeseries = self.get_timeseries(timeseries_type)
        self.log = self.log.bind(timeseries_type=timeseries_type.value)

        # Hospitalizations have a strange effect in the first few data points across many states.
        # Let's just drop those..
        if timeseries_type in (
            TimeseriesType.CURRENT_HOSPITALIZATIONS,
            TimeseriesType.NEW_HOSPITALIZATIONS,
        ):
            dates, times, timeseries = dates[2:], times[:2], timeseries[2:]

        # Remove Outliers Before Smoothing. Replaces a value if the current is more than 10 std
        # from the 14 day trailing mean and std
        timeseries_og = timeseries
        timeseries = replace_outliers(x=pd.Series(timeseries), log=self.log)

        not_round_smoothed = (
            timeseries.rolling(
                self.window_size, win_type="gaussian", min_periods=self.kernel_std, center=True
            ).mean(std=self.kernel_std)
            # .round()
        )
        smoothed = (
            timeseries.rolling(
                self.window_size, win_type="gaussian", min_periods=self.kernel_std, center=True
            )
            .mean(std=self.kernel_std)
            .round()
        )

        plt.close("all")
        plt.plot(dates, timeseries_og, label="OG")
        plt.plot(dates, timeseries, label="+remove outliers")
        plt.plot(dates, not_round_smoothed, label="+smoothed")
        plt.plot(dates, smoothed, label="+round")

        plt.legend()
        plt.title(timeseries_type)

        plt.savefig(str(timeseries_type) + "_cleaning.pdf")

        nonzeros = [idx for idx, val in enumerate(smoothed) if val != 0]

        if smoothed.empty:
            idx_start = 0
        elif max(smoothed) < smoothed_max_threshold:
            # skip the entire array.
            idx_start = len(smoothed)
        else:
            idx_start = nonzeros[0]

        smoothed = smoothed.iloc[idx_start:]
        original = timeseries.loc[smoothed.index]

        if plot:
            plt.scatter(
                dates[-len(original) :],
                original,
                alpha=0.3,
                label=timeseries_type.value.replace("_", " ").title() + "Shifted",
            )
            plt.plot(dates[-len(original) :], smoothed)
            plt.grid(True, which="both")
            plt.xticks(rotation=30)
            plt.xlim(min(dates[-len(original) :]), max(dates) + timedelta(days=2))
            plt.legend()

        return dates, times, smoothed

    def highest_density_interval(self, posteriors, ci):
        """
        Given a PMF, generate the confidence bands.

        Parameters
        ----------
        posteriors: pd.DataFrame
            Probability Mass Function to compute intervals for.
        ci: float
            Float confidence interval. Value of 0.95 will compute the upper and
            lower bounds.

        Returns
        -------
        ci_low: np.array
            Low confidence intervals.
        ci_high: np.array
            High confidence intervals.
        """
        posterior_cdfs = posteriors.values.cumsum(axis=0)
        low_idx_list = np.argmin(np.abs(posterior_cdfs - (1 - ci)), axis=0)
        high_idx_list = np.argmin(np.abs(posterior_cdfs - ci), axis=0)
        ci_low = self.r_list[low_idx_list]
        ci_high = self.r_list[high_idx_list]
        return ci_low, ci_high

    def get_posteriors(self, timeseries_type, plot=False):
        """
        Generate posteriors for R_t.

        Parameters
        ----------
        ----------
        timeseries_type: TimeseriesType
            New X per day (cases, deaths etc).
        plot: bool
            If True, plot a cool looking est of posteriors.

        Returns
        -------
        dates: array-like
            Input data over a subset of indices available after windowing.
        times: array-like
            Output integers since the reference date.
        posteriors: pd.DataFrame
            Posterior estimates for each timestamp with non-zero data.
        """
        dates, times, timeseries = self.apply_gaussian_smoothing(timeseries_type)
        if len(timeseries) == 0:
            return None, None, None, None

        # (1) Calculate Lambda (the Poisson likelihood given the data) based on
        # the observed increase from t-1 cases to t cases.
        lam = timeseries[:-1].values * np.exp((self.r_list[:, None] - 1) / self.serial_period)

        # (2) Calculate each day's likelihood over R_t
        likelihoods = pd.DataFrame(
            data=sps.poisson.pmf(timeseries[1:].values, lam),
            index=self.r_list,
            columns=timeseries.index[1:],
        )

        # (3) Create the Gaussian Matrix
        process_matrix = sps.norm(loc=self.r_list, scale=self.process_sigma).pdf(
            self.r_list[:, None]
        )

        # (3a) Normalize all rows to sum to 1
        process_matrix /= process_matrix.sum(axis=0)

        # (4) Calculate the initial prior. Gamma mean of "a" with mode of "a-1".
        prior0 = sps.gamma(a=2.5).pdf(self.r_list)
        prior0 /= prior0.sum()

        reinit_prior = sps.gamma(a=2).pdf(self.r_list)
        reinit_prior /= reinit_prior.sum()

        # Create a DataFrame that will hold our posteriors for each day
        # Insert our prior as the first posterior.
        posteriors = pd.DataFrame(
            index=self.r_list, columns=timeseries.index, data={timeseries.index[0]: prior0}
        )

        # We said we'd keep track of the sum of the log of the probability
        # of the data for maximum likelihood calculation.
        log_likelihood = 0.0

        # (5) Iteratively apply Bayes' rule
        for previous_day, current_day in zip(timeseries.index[:-1], timeseries.index[1:]):
            # (5a) Calculate the new prior
            current_prior = process_matrix @ posteriors[previous_day]

            # (5b) Calculate the numerator of Bayes' Rule: P(k|R_t)P(R_t)
            numerator = likelihoods[current_day] * current_prior

            # (5c) Calculate the denominator of Bayes' Rule P(k)
            denominator = np.sum(numerator)

            # Execute full Bayes' Rule
            if denominator == 0:
                # Restart the baysian learning for the remaining series.
                # This is necessary since otherwise NaN values
                # will be inferred for all future days, after seeing
                # a single (smoothed) zero value.
                #
                # We understand that restarting the posteriors with the
                # re-initial prior may incur a start-up artifact as the posterior
                # restabilizes, but we believe it's the current best
                # solution for municipalities that have smoothed cases and
                # deaths that dip down to zero, but then start to increase
                # again.

                posteriors[current_day] = reinit_prior
            else:
                posteriors[current_day] = numerator / denominator

            # Add to the running sum of log likelihoods
            log_likelihood += np.log(denominator)

        self.log_likelihood = log_likelihood

        if plot:
            plt.figure(figsize=(12, 8))
            plt.plot(posteriors, alpha=0.1, color="k")
            plt.grid(alpha=0.4)
            plt.xlabel("$R_t$", fontsize=16)
            plt.title("Posteriors", fontsize=18)
        start_idx = -len(posteriors.columns)

        return dates[start_idx:], times[start_idx:], posteriors, start_idx

    def get_available_timeseries(self, plot=True):
        # logging.info('GETTING TIMESERIES')
        available_timeseries = []

        # Get case and death series (index two returned by get_timeseries() result)
        IDX_OF_COUNTS = 2
        cases = self.get_timeseries(TimeseriesType.NEW_CASES.value)[IDX_OF_COUNTS]
        deaths = self.get_timeseries(TimeseriesType.NEW_DEATHS.value)[IDX_OF_COUNTS]

        if self.hospitalization_data_type:
            hosps = self.get_timeseries(TimeseriesType.NEW_HOSPITALIZATIONS.value)[IDX_OF_COUNTS]

        if np.sum(cases) > self.min_cases:
            available_timeseries.append(TimeseriesType.NEW_CASES)
            available_timeseries.append(TimeseriesType.RAW_NEW_CASES)

        if np.sum(deaths) > self.min_deaths:
            # logging.info('adding deaths')
            available_timeseries.append(TimeseriesType.NEW_DEATHS)
            available_timeseries.append(TimeseriesType.RAW_NEW_DEATHS)
            # logging.info('added deaths')

        if (
            self.hospitalization_data_type
            is load_data.HospitalizationDataType.CURRENT_HOSPITALIZATIONS
            and len(hosps > 3)
        ):
            # We have converted this timeseries to new hospitalizations.
            available_timeseries.append(TimeseriesType.NEW_HOSPITALIZATIONS)
        elif (
            self.hospitalization_data_type
            is load_data.HospitalizationDataType.CUMULATIVE_HOSPITALIZATIONS
            and len(hosps > 3)
        ):
            available_timeseries.append(TimeseriesType.NEW_HOSPITALIZATIONS)

        # logging.info(available_timeseries)
        # logging.info('about to plot')
        if plot:
            plt.close("all")
            for timeseries_type in available_timeseries:
                dates, times, timeseries = self.get_timeseries(timeseries_type)
                # logging.info('plotting')
                # logging.info(timeseries_type)
                # logging.info(timeseries)
                logging.info(f"series length:{len(timeseries)} date length: {len(dates)}")
                # logging.info(dates)
                plt.plot(dates, timeseries, label=timeseries_type, marker=".")
            plt.legend()
            # ax.y_scale("symlog")
            # logging.info('saved')
            plt.savefig("rawdata.pdf")
            plt.close("all")
        logging.info("PLOTTED")

        return available_timeseries

    def infer_all(self, plot=True, shift_deaths=0):
        """
        Infer R_t from all available data sources.

        Parameters
        ----------
        plot: bool
            If True, generate a plot of the inference.
        shift_deaths: int
            Shift the death time series by this amount with respect to cases
            (when plotting only, does not shift the returned result).

        Returns
        -------
        inference_results: pd.DataFrame
            Columns containing MAP estimates and confidence intervals.
        """
        logging.info("STARTING")
        df_all = None
        available_timeseries = []
        available_timeseries = self.get_available_timeseries()
        logging.info("timeseries: ")

        logging.info(available_timeseries)
        for timeseries_type in available_timeseries:
            dates, times, timeseries = self.get_timeseries(timeseries_type)
            df = pd.DataFrame()
            dates, times, posteriors, start_idx = self.get_posteriors(timeseries_type)
            if posteriors is not None:
                df[f"Rt_MAP__{timeseries_type.value}"] = posteriors.idxmax()
                for ci in self.confidence_intervals:
                    ci_low, ci_high = self.highest_density_interval(posteriors, ci=ci)

                    low_val = 1 - ci
                    high_val = ci
                    df[f"Rt_ci{int(math.floor(100 * low_val))}__{timeseries_type.value}"] = ci_low
                    df[f"Rt_ci{int(math.floor(100 * high_val))}__{timeseries_type.value}"] = ci_high

                df["date"] = dates
                df = df.set_index("date")
                df[f"{timeseries_type.value}"] = timeseries[start_idx:]
                # logging.info('DATAFRAME: ')
                # logging.info(df)

                if df_all is None:
                    df_all = df
                else:
                    df_all = df_all.merge(df, left_index=True, right_index=True, how="outer")

                # ------------------------------------------------
                # Compute the indicator lag using the curvature
                # alignment method.
                # ------------------------------------------------
                if (
                    timeseries_type
                    in (TimeseriesType.NEW_DEATHS, TimeseriesType.NEW_HOSPITALIZATIONS)
                    and f"Rt_MAP__{TimeseriesType.NEW_CASES.value}" in df_all.columns
                ):

                    # Go back up to 30 days or the max time series length we have if shorter.
                    last_idx = max(-21, -len(df))
                    series_a = df_all[f"Rt_MAP__{TimeseriesType.NEW_CASES.value}"].iloc[-last_idx:]
                    series_b = df_all[f"Rt_MAP__{timeseries_type.value}"].iloc[-last_idx:]

                    shift_in_days = self.align_time_series(series_a=series_a, series_b=series_b,)

                    df_all[f"lag_days__{timeseries_type.value}"] = shift_in_days
                    logging.debug(
                        "Using timeshift of: %s for timeseries type: %s ",
                        shift_in_days,
                        timeseries_type,
                    )
                    # Shift all the columns.
                    for col in df_all.columns:
                        if timeseries_type.value in col:
                            df_all[col] = df_all[col].shift(shift_in_days)
                            # Extend death and hopitalization rt signals beyond
                            # shift to avoid sudden jumps in composite metric.
                            #
                            # N.B interpolate() behaves differently depending on the location
                            # of the missing values: For any nans appearing in between valid
                            # elements of the series, an interpolated value is filled in.
                            # For values at the end of the series, the last *valid* value is used.
                            logging.debug("Filling in %s missing values", shift_in_days)
                            df_all[col] = df_all[col].interpolate(
                                limit_direction="forward", method="linear"
                            )

        if df_all is not None and "Rt_MAP__new_deaths" in df_all and "Rt_MAP__new_cases" in df_all:
            df_all["Rt_MAP_composite"] = np.nanmean(
                df_all[["Rt_MAP__new_cases", "Rt_MAP__new_deaths"]], axis=1
            )
            # Just use the Stdev of cases. A correlated quadrature summed error
            # would be better, but is also more confusing and difficult to fix
            # discontinuities between death and case errors since deaths are
            # only available for a subset. Systematic errors are much larger in
            # any case.
            df_all["Rt_ci95_composite"] = df_all["Rt_ci95__new_cases"]

        elif df_all is not None and "Rt_MAP__new_cases" in df_all:
            df_all["Rt_MAP_composite"] = df_all["Rt_MAP__new_cases"]
            df_all["Rt_ci95_composite"] = df_all["Rt_ci95__new_cases"]

        if plot and df_all is not None:
            plt.figure(figsize=(10, 6))

            if "Rt_MAP_composite" in df_all:
                plt.scatter(
                    df_all.index,
                    df_all["Rt_MAP_composite"],
                    alpha=1,
                    s=25,
                    color="yellow",
                    label="Inferred $R_{t}$ Web",
                    marker="d",
                )
            plt.hlines([1.0], *plt.xlim(), alpha=1, color="g")
            plt.hlines([1.1], *plt.xlim(), alpha=1, color="gold")
            plt.hlines([1.3], *plt.xlim(), alpha=1, color="r")

            if "Rt_ci5__new_deaths" in df_all:
                plt.fill_between(
                    df_all.index,
                    df_all["Rt_ci5__new_deaths"],
                    df_all["Rt_ci95__new_deaths"],
                    alpha=0.2,
                    color="firebrick",
                )
                plt.scatter(
                    df_all.index,
                    df_all["Rt_MAP__new_deaths"].shift(periods=shift_deaths),
                    alpha=1,
                    s=25,
                    color="firebrick",
                    label="New Deaths",
                )

            if "Rt_ci5__new_cases" in df_all:
                plt.fill_between(
                    df_all.index,
                    df_all["Rt_ci5__new_cases"],
                    df_all["Rt_ci95__new_cases"],
                    alpha=0.2,
                    color="steelblue",
                )
                plt.scatter(
                    df_all.index,
                    df_all["Rt_MAP__new_cases"],
                    alpha=1,
                    s=25,
                    color="steelblue",
                    label="New Cases",
                    marker="s",
                )

            if "Rt_ci5__new_hospitalizations" in df_all:
                plt.fill_between(
                    df_all.index,
                    df_all["Rt_ci5__new_hospitalizations"],
                    df_all["Rt_ci95__new_hospitalizations"],
                    alpha=0.4,
                    color="darkseagreen",
                )
                plt.scatter(
                    df_all.index,
                    df_all["Rt_MAP__new_hospitalizations"],
                    alpha=1,
                    s=25,
                    color="darkseagreen",
                    label="New Hospitalizations",
                    marker="d",
                )

            plt.hlines([1.0], *plt.xlim(), alpha=1, color="g")
            plt.hlines([1.1], *plt.xlim(), alpha=1, color="gold")
            plt.hlines([1.3], *plt.xlim(), alpha=1, color="r")

            plt.xticks(rotation=30)
            plt.grid(True)
            plt.xlim(df_all.index.min() - timedelta(days=2), df_all.index.max() + timedelta(days=2))
            plt.ylim(-1, 4)
            plt.ylabel("$R_t$", fontsize=16)
            plt.legend()
            plt.title(self.display_name, fontsize=16)

            output_path = get_run_artifact_path(self.fips, RunArtifact.RT_INFERENCE_REPORT)
            plt.savefig(output_path, bbox_inches="tight")
            # plt.close()

        if df_all is None or df_all.empty:
            logging.warning("Inference not possible for fips: %s", self.fips)

        df_all.to_csv("df_all.csv")

        logging.info("about to run forecast rt")

        # self.forecast_rt(df_all)
        return df_all

    def forecast_rt(self, df_all):
        logging.info("starting")
        """
        predict r_t for 14 days into the future
        
        Parameters
        ___________
        df_all: dataframe with dates, new_cases, new_deaths, and r_t values

        Potential todo: add more features

        Returns
        __________
        dates and forecast r_t values

        """
        logging.info("beginning forecast")

        # Convert dates to what day of 2020 it corresponds to for Forecast
        SIM_DATE_NAME = "sim_day"
        df_all["sim_day"] = (
            df_all.index - self.ref_date
        ).days + 1  # set first day of year to 1 not zero --- check why this varies for Idaho number of entries not the same for corrected/notcorrected
        # slim dataframe to only variables used in prediction
        PREDICT_VARIABLE = "Rt_MAP_composite"
        FORECAST_VARIABLES = ["sim_day", "raw_new_cases", "raw_new_deaths", "Rt_MAP_composite"]

        df_forecast = df_all[FORECAST_VARIABLES].copy()

        df_forecast.to_csv("df_forecast.csv", na_rep="NaN")

        # Fill empty values with zero
        df_forecast.replace(r"\s+", np.nan, regex=True).replace("", np.nan)

        # Split into train and test before normalizing to avoid data leakage
        # TODO: Test set will actually be entire series
        TRAIN_SIZE = 0.8
        train_set_length = int(len(df_forecast) * TRAIN_SIZE)
        train_set = df_forecast[:train_set_length]
        test_set = df_forecast[
            train_set_length:
        ]  # this is really the entire series TODO maybe find a better way to code this
        logging.info("train set")
        logging.info(train_set)
        logging.info("test set")
        logging.info(test_set)
        logging.info(f"train_size: {len(train_set.index)} test_size: {len(test_set.index)}")

        # Normalize Inputs for training
        scalers_dict = {}
        scaled_values_dict = {}
        for columnName, columnData in train_set.iteritems():
            # there is probably a better way to do this
            scaler = preprocessing.MinMaxScaler(feature_range=(-1, 1))
            reshaped_data = columnData.values.reshape(-1, 1)

            scaler = scaler.fit(reshaped_data)
            scaled_values = scaler.transform(reshaped_data)

            # add these columns to dataframe
            train_set.loc[:, f"{columnName}_scaled"] = scaled_values

            # update dictionary for later use to unscale data
            scalers_dict.update({columnName: scaler})
            scaled_values_dict.update({columnName: scaled_values})

        train_set.to_csv("train_set_scaled.csv")
        plt.close("all")
        for variable in scaled_values_dict:
            plt.plot(scaled_values_dict["sim_day"], scaled_values_dict[variable], label=variable)

        plt.legend()
        plt.savefig("scaledfig.pdf")

        # Get features and labels
        MIN_NUMBER_OF_DAYS = (
            30  # I don't think it makes sense to predict anything until we have a month of data
        )
        PREDICT_DAYS = 7
        # Create list of dataframes for testing
        logging.info("creating dataset list")
        train_df_samples = self.create_df_list(train_set, MIN_NUMBER_OF_DAYS, PREDICT_DAYS)
        logging.info("done")

        # check if dictionary of scalers works
        logging.info("scaled everything")
        logging.info(scalers_dict)

        return

    @staticmethod
    def create_df_list(df, min_days, predict_days):
        logging.info("CREATING DF LIST")
        df_list = list()
        for i in range(len(df.index)):
            if i < predict_days + min_days:  # only keep df if it has min number of entries
                continue
            else:
                df_list.append(df[0:i].copy())  # here could also create week and month predictions
        return df_list

    @staticmethod
    def ewma_smoothing(series, tau=5):
        """
        Exponentially weighted moving average of a series.

        Parameters
        ----------
        series: array-like
            Series to convolve.
        tau: float
            Decay factor.

        Returns
        -------
        smoothed: array-like
            Smoothed series.
        """
        exp_window = signal.exponential(2 * tau, 0, tau, False)[::-1]
        exp_window /= exp_window.sum()
        smoothed = signal.convolve(series, exp_window, mode="same")
        return smoothed

    @staticmethod
    def align_time_series(series_a, series_b):
        """
        Identify the optimal time shift between two data series based on
        maximal cross-correlation of their derivatives.

        Parameters
        ----------
        series_a: pd.Series
            Reference series to cross-correlate against.
        series_b: pd.Series
            Reference series to shift and cross-correlate against.

        Returns
        -------
        shift: int
            A shift period applied to series b that aligns to series a
        """
        shifts = range(-21, 5)
        valid_shifts = []
        xcor = []
        np.random.seed(InferRtConstants.RNG_SEED)  # Xcor has some stochastic FFT elements.
        _series_a = np.diff(series_a)

        for i in shifts:
            series_b_shifted = np.diff(series_b.shift(i))
            valid = ~np.isnan(_series_a) & ~np.isnan(series_b_shifted)
            if len(series_b_shifted[valid]) > 0:
                xcor.append(signal.correlate(_series_a[valid], series_b_shifted[valid]).mean())
                valid_shifts.append(i)
        if len(valid_shifts) > 0:
            return valid_shifts[np.argmax(xcor)]
        else:
            return 0

    @classmethod
    def run_for_fips(cls, fips):
        try:
            engine = cls(fips)
            return engine.infer_all()
        except Exception as e:
            sentry_sdk.capture_exception(e)
            return None


def replace_outliers(
    x,
    log,
    local_lookback_window=InferRtConstants.LOCAL_LOOKBACK_WINDOW,
    z_threshold=InferRtConstants.Z_THRESHOLD,
    min_mean_to_consider=InferRtConstants.MIN_MEAN_TO_CONSIDER,
):
    """
    Take a pandas.Series, apply an outlier filter, and return a pandas.Series.

    This outlier detector looks at the z score of the current value compared to the mean and std
    derived from the previous N samples, where N is the local_lookback_window.

    For points where the z score is greater than z_threshold, a check is made to make sure the mean
    of the last N samples is at least min_mean_to_consider. This makes sure we don't filter on the
    initial case where values go from all zeros to a one. If that threshold is met, the value is
    then replaced with the linear interpolation between the two nearest neighbors.


    Parameters
    ----------
    x
        Input pandas.Series with the values to analyze
    log
        Logger instance
    local_lookback_window
        The length of the rolling window to look back and calculate the mean and std to baseline the
        z score. NB: We require the window to be full before returning any result.
    z_threshold
        The minimum z score needed to trigger the replacement
    min_mean_to_consider
        Threshold to skip low n cases, especially the degenerate case where a long list of zeros
        becomes a 1. This requires that the rolling mean of previous values must be greater than
        or equal to min_mean_to_consider to be replaced.
    Returns
    -------
    x
        pandas.Series with any triggered outliers replaced
    """

    # Calculate Z Score
    r = x.rolling(window=local_lookback_window, min_periods=local_lookback_window, center=False)
    m = r.mean().shift(1)
    s = r.std(ddof=0).shift(1)
    z_score = (x - m) / (s + EPSILON)
    possible_changes_idx = np.where(z_score > z_threshold)[0]
    changed_idx = []
    changed_value = []
    changed_snippets = []

    for idx in possible_changes_idx:
        if m[idx] > min_mean_to_consider:
            changed_idx.append(idx)
            changed_value.append(int(x[idx]))
            slicer = slice(idx - local_lookback_window, idx + local_lookback_window)
            changed_snippets.append(x[slicer].astype(int).tolist())
            try:
                x[idx] = np.mean([x[idx - 1], x[idx + 1]])
            except IndexError:  # Value to replace can be newest and fail on x[idx+1].
                # If so, just use previous.
                x[idx] = x[idx - 1]

    if len(changed_idx) > 0:
        log.info(
            event="Replacing Outliers:",
            outlier_values=changed_value,
            z_score=z_score[changed_idx].astype(int).tolist(),
            where=changed_idx,
            snippets=changed_snippets,
        )

    return x


def run_state(state, states_only=False):
    """
    Run the R_t inference for each county in a state.

    Parameters
    ----------
    state: str
        State to run against.
    states_only: bool
        If True only run the state level.
    """
    state_obj = us.states.lookup(state)
    df = RtInferenceEngine.run_for_fips(state_obj.fips)
    output_path = get_run_artifact_path(state_obj.fips, RunArtifact.RT_INFERENCE_RESULT)
    if df is None or df.empty:
        logging.error("Empty dataframe encountered! No RtInference results available for %s", state)
    else:
        df.to_json(output_path)

    # Run the counties.
    if not states_only:
        all_fips = load_data.get_all_fips_codes_for_a_state(state)

        # Something in here doesn't like multiprocessing...
        rt_inferences = all_fips.map(lambda x: RtInferenceEngine.run_for_fips(x)).tolist()

        for fips, rt_inference in zip(all_fips, rt_inferences):
            county_output_file = get_run_artifact_path(fips, RunArtifact.RT_INFERENCE_RESULT)
            if rt_inference is not None:
                rt_inference.to_json(county_output_file)


def run_county(fips):
    """
    Run the R_t inference for each county in a state.

    Parameters
    ----------
    fips: str
        County fips to run against
    """
    if not fips:
        return None

    df = RtInferenceEngine.run_for_fips(fips)
    county_output_file = get_run_artifact_path(fips, RunArtifact.RT_INFERENCE_RESULT)
    if df is not None and not df.empty:
        df.to_json(county_output_file)
