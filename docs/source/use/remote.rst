
===============================
Working with remote mcloud
===============================

Initial application deployment
===============================

Test you have connection with your server::

    $ mcloud -h <ip-here> list

If, you see empty table from remote server, then it's working.

We can initialize a nwe application.

Go to folder with your application (we will use flask-redis) example.
And initialize application on remote machine::

    $ mcloud -h <ip-here> init flask-redis mcloud.yml

flask-redis is name of your application on remote server.

MCloud will create a new application there. Now you can push initial version
of your application to remote server::

    $ mcloud sync . flask-redis@<ip-here>

During sync mcloud will ask to review changes and confirmation to de[ploy the things.

As application is synced, you can start it::

    $ mcloud -h <ip-here> start


Updating deployed application
=======================================

Updating applications is as easy as uploading changes::

    $ mcloud sync . flask-redis@<ip-here>

And restarting application::

    $ mcloud -h <ip-here> restart flask-redis

If you changed configuration in mcloud.yml, you may need to deploy changes to
container structure as well::

    $ mcloud -h <ip-here> config --update flask-redis

