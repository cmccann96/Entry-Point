def column_sum(df):

    col_list = list(df)
    
    col_list.remove('Dates')

    total = list(df[col_list].sum())
    total_sum = sum(total)
    return total_sum
