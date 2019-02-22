FROM elasticsearch:6.6.1
RUN yes | bin/elasticsearch-plugin install repository-s3
RUN bin/elasticsearch-keystore create
RUN echo dummydummy | bin/elasticsearch-keystore add --stdin s3.client.default.access_key
RUN echo dummydummy | bin/elasticsearch-keystore add --stdin s3.client.default.secret_key
