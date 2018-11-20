import json
import os
import shutil
import glob
import re
import warnings

import pandas as pd
import numpy as np
from natsort import natsorted  # install using: conda install -c anaconda natsort


def reduce_df(path, output, nrows=None, chunksize=20000):
    """ Load Google analytics data from JSON into a Pandas.DataFrame. """
    if nrows and chunksize:
        msg = "Reading {} rows in chunks of {}. We are gonna need {} chunks"
        print(msg.format(nrows, chunksize, nrows / chunksize))

    temp_dir = "../data/temp"
    if not os.path.exists(temp_dir):
        os.makedirs(temp_dir)

    JSON_COLUMNS = ['device', 'geoNetwork', 'totals', 'trafficSource'] # noqa

    i = 0
    for chunk in pd.read_csv(path,
                             converters={column: json.loads for column in JSON_COLUMNS},
                             dtype={'fullVisitorId': 'str'},
                             nrows=nrows,
                             chunksize=chunksize):

        chunk = chunk.reset_index()

        # Normalize JSON columns
        for column in JSON_COLUMNS:
            column_as_df = pd.io.json.json_normalize(chunk[column])
            chunk = chunk.drop(column, axis=1).merge(column_as_df, right_index=True, left_index=True)

        # Parse date
        chunk['date'] = chunk['date'].apply(lambda x: pd.datetime.strptime(str(x), '%Y%m%d'))

        # Only keep relevant columns
        cols = ['date', 'fullVisitorId', 'operatingSystem', 'country', 'browser',
                'pageviews', 'transactions', 'visits', 'transactionRevenue', 'visitStartTime']
        try:
            chunk = chunk[cols]
        except KeyError as e:
            # Regex magic to find exactly which columns were not found.
            # Might be different in Python 3, be careful!
            missing_cols = list(re.findall(r"'(.*?)'", e.args[0]))
            for col in missing_cols:
                print("Column {} was not found in chunk {}, filling with zeroes".format(col, i))
                chunk[col] = [0] * len(chunk)
            chunk = chunk[cols]

        print("Loaded chunk {}, Shape is: {}".format(i, chunk.shape))
        chunk.to_csv(os.path.join(temp_dir, str(i) + ".csv"), encoding='utf-8', index=False)
        i += 1

    print("Finished all chunks, now concatenating")
    files = glob.glob(os.path.join(temp_dir, "*.csv"))
    with open(output, 'wb') as outfile:
        for i, fname in enumerate(files):
            with open(fname, 'rb') as infile:
                # Throw away header on all but first file
                if i != 0:
                    infile.readline()
                # Block copy rest of file from input to output without parsing
                shutil.copyfileobj(infile, outfile)

    print("Deleting temp folder {}".format(temp_dir))
    shutil.rmtree(temp_dir)


def aggregate(df):
    """Group and pivot the dataframe so that we have one row per visitor.

    This row includes features of two types:
      * Dynamic. These are repeated for each month and capture the time-series like behavior.
                 Some examples are the revenue and visits per month, for every month in the dataset.

      * Static. These exist once per user and correspond to relatively constant properties.
                Examples include the person's country, OS and browser.

    Parameters
    ----------
    df : pd.DataFrame
        The original reduced dataframe - see the `reduce_df` function

    Returns
    -------
    pd.DataFrame
        The same dataframe grouped and pivoted, where each visitor is a single row.

    """
    # Long to wide pivot - one row per visitor.
    dynamic_columns = ['transactionRevenue', 'visits', 'transactions', 'pageviews']
    wide = pd.pivot_table(df, index='fullVisitorId', columns='date_temp', values=dynamic_columns)

    # Collapse multi-index.
    wide.columns = wide.columns.to_series().str.join('_')
    wide.reset_index(inplace=True)

    # Now let's also get the static columns.
    static_columns = list(set(df.columns) - set(dynamic_columns) - {"date"} - {"date_temp"})
    static = df[static_columns]

    # Regroup on visitor.
    print("Regroup again to get the static fields - this will take a while")
    static_grouped = static.groupby("fullVisitorId", as_index=False)[static_columns].sum()
    # Merge dynamic and static features.
    df = static_grouped.merge(wide, on="fullVisitorId", how="inner")
    return df[natsorted(df.columns, key=lambda y: y.lower())]


def group_data(df):
    """ Aggregates the data per user and date
    """
    def most_frequent(x):
        return x.value_counts().index[0]

    agg = {
            "operatingSystem": most_frequent,
            "country": most_frequent,
            "browser": most_frequent,
            "weekday": most_frequent,
            "pageviews": sum,
            "transactions": sum,
            "visits": sum,
            "transactionRevenue": sum,
            "visitStartTime": "first",
            "date": "first"
        }

    def agg_date(s):
        try:
            date = pd.datetime.strptime(str(s), '%Y-%m-%d')
            return "{}_{}".format(date.month, date.year)
        except ValueError:
            return "{}_{}".format(s.month, s.year)

    df["date_temp"] = df["date"].apply(agg_date)

    print("First grouping, this will take a while")
    return df.groupby(["fullVisitorId", "date_temp"], as_index=False).agg(agg)


def split_data(train, test, x_train_dates=('2016-08-01', '2017-11-30'), y_test_dates=('2017-12-01', '2018-01-31'),
               x_test_dates=('2017-08-01', '2018-11-30'), selec_top_per=0.5, max_cat=10):
    """A funtion to plit and preprocess the datasets

    """
    merged = pd.concat([train, test], sort=False)
    merged['transactions'].fillna(0, inplace=True)
    merged['transactionRevenue'].fillna(0.0, inplace=True)
    # Create some features
    merged['date'] = merged['date'].apply(lambda x: pd.datetime.strptime(str(x), '%Y-%m-%d'))
    merged['weekday'] = merged['date'].apply(lambda x: x.weekday())

    # Aggregate the dataset
    merged = group_data(merged)
    # Reduce categories on static columns
    OHE_reduce = ['operatingSystem', 'country', 'browser']  # noqa
    merged = reduce_categories(merged, OHE_reduce, selec_top_per, max_cat)
    # create OHE
    merged = one_hot_encode_categoricals(merged)

    # Split in train and test
    x_train = merged[(merged["date"] >= x_train_dates[0]) & (merged["date"] <= x_train_dates[1])]
    y_train = merged.loc[(merged["date"] >= y_test_dates[0]) & (merged["date"] <= y_test_dates[1]), ['transactionRevenue', 'fullVisitorId']]
    y_train['target'] = y_train.groupby(['fullVisitorId'], as_index=False)['transactionRevenue'].sum()['transactionRevenue']
    y_train['target'] = np.log(y_train['target'] + 1)
    del y_train['transactionRevenue']
    x_test = merged[(merged["date"] >= x_test_dates[0]) & (merged["date"] <= x_test_dates[1])]

    # create dynamic features
    x_train = aggregate(x_train)
    x_train.columns = [x.replace('2016', '1') for x in x_train.columns]
    x_train.columns = [x.replace('2017', '2') for x in x_train.columns]

    x_test = aggregate(x_test)
    x_test.columns = [x.replace('2017', '1') for x in x_test.columns]
    x_test.columns = [x.replace('2018', '2') for x in x_test.columns]

    # Guarantee that the names are the same in train and test
    names_train = set(x_train.columns.values)
    names_test = set(x_test.columns.values)
    names = list(names_train.intersection(names_test))

    # Guarantee the same users
    names_x = x_train.columns.values
    names_y = y_train.columns.values
    merged = x_train.merge(y_train, on="fullVisitorId", how="inner")
    x_train = merged[names_x]
    y_train = merged[names_y]

    return x_train[names], y_train, x_test[names]


def reduce_categories(df, ohe_reduce, selec_top_per, max_cat):
    """ Reduce the number of catergories based 'target' values

    Params
    ------
    selec_top_per: float
    The percentage of top categories to be included.

    max_cat: int
    The maximun number of categories to have.

    Return
    ------
    pd.DataFrame
        df with the categories reduced
    """

    target_name = 'transactionRevenue'
    for col in ohe_reduce:
        print('Reducing the OHE of {}'.format(col))
        if len(df[col].unique()) > max_cat:
            top = df.groupby(col, as_index=False)[target_name].sum() \
                .sort_values(by=[target_name], ascending=False).reset_index(drop=True)
            top['per'] = np.cumsum(top[target_name])/np.sum(top[target_name])
            top_names = top.loc[top['per'] <= selec_top_per, col]
            # To have more than one category
            if len(top_names) < max_cat:
                top_names = top.loc[0:min(max_cat, len(top[col])), col]
            df.loc[~df[col].isin(top_names), col] = 'other category'

    return df


def load_train_test_dataframes(data_dir, x_train_file_name='preprocessed_x_train.csv',
                               y_train_file_name='preprocessed_y_train.csv',
                               x_test_file_name='preprocessed_x_test.csv', nrows_train=None,
                               nrows_test=None, selec_top_per=0.5, max_cat=5):
    """ Load the train and test DataFrames resulting from preprocessing. """
    x_train = pd.read_csv(os.path.join(data_dir, x_train_file_name),
                          dtype={"fullVisitorId": str},
                          nrows=nrows_train)

    y_train = pd.read_csv(os.path.join(data_dir, y_train_file_name),
                          dtype={"fullVisitorId": str})

    x_test = pd.read_csv(os.path.join(data_dir + x_test_file_name),
                         dtype={"fullVisitorId": str},
                         nrows=nrows_test)

    x_train['date'] = x_train['date'].apply(lambda x: pd.datetime.strptime(str(x), '%Y-%m-%d'))
    x_test['date'] = x_test['date'].apply(lambda x: pd.datetime.strptime(str(x), '%Y-%m-%d'))
    return x_train, y_train, x_test


def one_hot_encode_categoricals(data):
    """ Transform categorical data to one-hot encoding and
        aggregate per customer.

    params
    ------
    data: DataFrame to transform.
    categorical_columns: array of column names indicating the
        columns to transform to one-hot encoding.

    notes
    -----
    The resulting columns are named as
    <original column name>_<original value>.

    Assumes the column fullVisitorId as grouper

    return
    ------
    The one-hot encoded DataFrame.
    """
    OHE = ['operatingSystem', 'country', 'browser', 'weekday']  # noqa
    for col in OHE:
        if data[col].dtypes in ["int64", "float64"]:
            warnings.warn("Column {} converted to from numeric to category".format(col))
            data[col] = data[col].astype("category")
        print('Creating the OHE of {}'.format(col))
        # create categories
        data = pd.concat([data, pd.get_dummies(data[col], prefix='category_')], sort=False)
        del data[col]
    return data


def summarize_numerical_data(data, cols, aggregation):
    """ Aggregate the numerical columns in the data per customer by
        summarizing / describing their values.

    Parameters
    ----------
    data: the DataFrame to aggregate.
    cols_to_describe: array-like of column names for which to compute
        descriptive measures such as mean, min, max, std, sum.
    cols_to_sum: array-like of column names for which to only compute
        the sum.

    Notes
    -----
    Aggregates by the column "fullVisitorId", so this column must
    be present.

    Returns
    -------
    The aggregated data with one row per customer and several columns for
    every column in the original data: min, max, mean, std, and sum for the
    columns in 'cols_to_describe' and the sum for the columns in 'cols_to_sum'
    """
    # describe columns
    data_describe = data.groupby('fullVisitorId')[cols] \
                        .agg(aggregation)
    data_describe.columns = ['_'.join(col) for col in data_describe.columns]
    data_describe[data_describe.columns[data_describe.columns.str.contains('std')]] \
        .fillna(0, inplace=True)

    return data_describe


def get_means_of_booleans(data, boolean_cols):
    """ Put boolean_cols of the data in a uniform format and
        compute the mean per customer.

    Parameters
    ----------
    data: The DataFrame.
    boolean_cols: array-like of column names with boolean values to
        process.

    Returns
    -------
    DataFrame with a row for each customer and columns presenting
    the percentage True for every column in boolean_cols.
    """
    # Some values are given in True/False, some in 1/NaN, etc.
    # Here we unify this to 1 and 0.
    data[boolean_cols] *= 1
    data[boolean_cols] = data[boolean_cols].fillna(0)
    # Calculate the percentage of 1s for each fullVisitorId
    data_bool = data.groupby(['fullVisitorId'])[boolean_cols].mean()
    data_bool = data_bool.add_suffix('_avg')
    return data_bool


def nrmonths(start, end):
    """Fucntion that returns the number of months between two datetimes.

    Parameters
    ----------
    start, end: pd.Timestamp

    Returns
    -------
    Number of months difference between start and end rounded to integers.
    """
    return int(np.floor((end - start) / np.timedelta64(1, 'M')))


def get_dynamic(data, cols, method, timewindow='monthly'):
    """ Get values for columns over time.

    For now only the option to add dynamic features per month is described.

    Parameters
    ----------
    cols: list(str),
        The columns to get the dynamic values for.
    method: function,
        How to aggregate the values (e.g., 'sum', 'mean', 'count').
    timewindow: str, one of {'monthly'},
        Only monthly aggregation supported right now.

    Returns
    -------
    The aggregated data.
    """
    if timewindow == 'monthly':
        data_dynamic = pd.pivot_table(data, index='fullVisitorId', values=cols, columns='nr_months_ago',
                                      aggfunc=method, fill_value=0)
        data_dynamic.columns = [str(x[0]) + '_' + str(x[1]) + '_' + method for x in data_dynamic.columns]
    else:
        print('This is not a possible timewindow')
    return data_dynamic


def add_datetime_features(data, date_col="date"):
    """ Calculate the time between first and last visit.

    Parameters
    ----------
    data: The DataFrame.
    date_col: String, the name of the column in 'data' that
        represents the date of the visit.

    Returns
    -------
    DataFrame with a row for each fullVisitorId in data and
    columns with the dates of their first and last visits.
    """
    data[date_col] = pd.to_datetime(data[date_col])
    data_date = data.groupby(['fullVisitorId'])[date_col] \
                    .agg(['min', 'max'])

    data_date['days_first_to_last_visit'] = \
        (data_date['max'] - data_date['min']).dt.days

    del data_date['max']
    del data_date['min']

    return data_date


def add_mean_time_between_visits(df):
    """ Add the mean inter-visit time to the DataFrame.

    Parameters
    ----------
    df: DataFrame where every row represents a customer.

    Returns
    -------
    The same DataFrame with an additional column 'mean_intervisit_time'
    that holds the mean time between two consecutive visits of the
    customer.
    """
    safe = df["totalVisits_mean"] > 1
    intervisit_time = np.zeros(len(df))
    intervisit_time[safe] = df["days_first_to_last_visit"][safe] \
        / (df["totalVisits_mean"][safe]-1)
    df["mean_intervisit_time"] = intervisit_time
    return df


def aggregate_data_per_customer(data, startdate_y, startdate_x):
    """ Aggregate the data per customer by one-hot encoding categorical
        variables and summarizing numerical variables.

    Parameters
    ----------
    data: DataFrame
        The data to aggregate.
    target_col_present: boolean
        Indicates whether the target column 'target' is in the data,
        so put this to True for the train data and False for test.

    Returns
    -------
    The aggregated DataFrame with one row per customer and
    a shit load of columns.
    """

    # Specify what to do with each column
    # Static
    OHE = ['channelGrouping', 'browser', 'deviceCategory', 'operatingSystem', 'city', 'continent',  # noqa
           'country', 'metro', 'region', 'subContinent', 'adContent',
           'adwordsClickInfo.adNetworkType', 'adwordsClickInfo.page', 'adwordsClickInfo.slot',
           'campaign', 'medium', 'source_cat', 'weekday', 'visitHour']
    booleans = ['isMobile', 'adwordsClickInfo.isVideoAd', 'isTrueDirect', 'keyword.isGoogle', 'keyword.isYouTube']
    cat_nunique = ['networkDomain']
    num_mean = ['totalVisits', 'keyword.mistakes_Google', 'keyword.mistakes_YouTube']

    # Dynamic
    monthly_count = ['bounces', 'newVisits']
    monthly_mean = ['hits', 'pageviews', 'target']
    monthly_sum = ['hits', 'pageviews', 'target']

    # Pre-process static data
    print("Summarizing the static variables...")
    data_categoricals = one_hot_encode_categoricals(data, OHE)
    # For the columns in 'unique_values', there is only one value per customer
    # and the rest is NaN, so we need just a 1 for the value and remove the NaN columns
    data_categoricals = \
        data_categoricals.loc[:, ~data_categoricals.columns.str.contains("NaN")]

    # categorical data with large numbers of unique values are dealt
    # with by only taking the number of unique values per customer
    data_diff = data.groupby(['fullVisitorId'])[cat_nunique] \
                    .nunique().add_suffix('_#diff')

    # handle booleans by taking the mean
    data_bools = get_means_of_booleans(data, booleans)

    # Describ num columns by getting the mean
    data_numericals = summarize_numerical_data(data, num_mean, ['mean'])

    # pre-process dynamic data
    print("Summarizing the dynamic variables...")
    startdate_y = pd.datetime.strptime(startdate_y, '%Y-%m-%d')
    startdate_x = pd.datetime.strptime(startdate_x, '%Y-%m-%d')
    data['nr_months_ago'] = data.apply(lambda row: nrmonths(row['date'], startdate_y), axis=1)
    data_dynamic_mean = get_dynamic(data, monthly_mean, 'mean', timewindow='monthly')
    data_dynamic_count = get_dynamic(data, monthly_count, 'count', timewindow='monthly')
    data_dynamic_sum = get_dynamic(data, monthly_sum, 'sum', timewindow='monthly')

    # Add dynamic feature with the number of visits
    data_dynamic_visits = one_hot_encode_categoricals(data, ['nr_months_ago'])

    # create datetime features: time between first and last visit
    # and mean time between consecutive visits
    data_dates = add_datetime_features(data)

    # merge
    print("Putting it all together..")
    df = pd.concat([data_categoricals, data_diff, data_bools, data_numericals, data_dynamic_mean,
                    data_dynamic_count, data_dynamic_sum, data_dynamic_visits, data_dates], axis=1)

    # add mean time between visits
    df = add_mean_time_between_visits(df)
    print("Done")

    return df


def ohe_explicit(df):
    """Partly one hot encodes specific categorical features.

    Instead of one hot encoding categorical columns with many distinct values (200+) we instead only create
    boolean columns corresponding to specific values based on two criteria:
        * Statistical Significance. These values should not be rare, else we are overfitting.
        * Predictive Power. These values should have conditional averages considerably different from the global
          target mean.

    The choice of features and values has been made manually during EDA and is subject to change in case we come
    up with better insights in the future.

    Examples
    --------
    >>> train = load("../data/train.csv")
    >>> check = ohe_explicit(train)
    >>> check.head()

    Parameters
    ----------
    df : pd.DataFrame
        A dataframe including the raw categorical features for Country and City.

    Returns
    -------
    pd.DataFrame
        The original categorical columns are replaced by one hot columns for specific values only.

    """
    countries = ["United States"]
    for country in countries:
        df["country_" + country] = df["country"].apply(lambda c: c == country)

    df.drop("country", axis=1, inplace=True)

    cities = ["New York", "Chicago", "Austin", "Seattle", "Palo Alto", "Toronto"]
    for city in cities:
        df["city_" + city] = df["city"].apply(lambda c: c == city)

    df.drop("city", axis=1, inplace=True)
    return df
