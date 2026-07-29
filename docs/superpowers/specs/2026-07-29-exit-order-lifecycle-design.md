# Exit order lifecycle design

## Goal

Improve passive exit execution for unpaired inventory without changing the
live process, forced-hedge criteria, or ordinary two-sided market-making
orders.  In particular, avoid losing queue priority by repeatedly replacing an
unchanged exit sell order.

## Scope

Add a dedicated exit-order GTD lifetime.  Normal quote orders continue using
`quoting.order_ttl_secs`; exit orders use a new risk configuration value with a
longer default lifetime.

## Behaviour

1. An existing exit order remains untouched when its token, price, and size
   match the desired exit and it is not close to its own GTD expiry.
2. Refresh an exit order only when its direction, price, or size changes, when
   it is close to expiry, or when the position no longer needs an exit order.
3. Exit replacement stays cancel-before-post.  Unlike ordinary quote refreshes,
   it must never briefly place two sell orders because either could fill and
   oversell the residual inventory.
4. Exit orders keep the GTD dead-man switch: after a crash they still expire.
   They are not converted to permanent/GTC orders.

## Configuration

`risk.exit_order_ttl_secs` controls the exit-order lifetime.  The initial
default is 600 seconds.  Its refresh margin remains the existing broker
margin, so a stable exit preserves queue position for most of its ten-minute
lifetime.

## Verification

Add focused broker tests proving that an unchanged exit order is retained
before its dedicated refresh window and replaced after that window.  Keep the
existing test proving changed exit quantity replaces the order.  Run the
focused tests and then the full test suite.
