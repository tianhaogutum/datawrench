{{ config(materialized='table') }}

select
    order_id,
    cast(customer_id as integer) as customer_id,
    amount,
    ordered_at
from {{ ref('stg_orders') }}
