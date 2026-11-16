select
    id as order_id,
    customer_id,
    amount,
    created_at as ordered_at
from {{ source('shop', 'raw_orders') }}
